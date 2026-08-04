# -*- coding: utf-8 -*-
"""
Бот доступа в клуб Creator Lab с оплатой через ЮMoney.

Два тарифа:
  - month   : подписка на 1 месяц (по умолчанию 699р)
  - forever : разовая покупка навсегда (по умолчанию 9990р)

Админ может изменять цены прямо через команды в Telegram:
  /setprice month 699
  /setprice forever 9990
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
from typing import Optional, Any

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
)

# ---------------------------------------------------------------------------
# Настройки (всё через переменные окружения)
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

CLUB_CHAT_ID = int(os.getenv("CLUB_CHAT_ID", "-1003973853516"))  # id клуба (с -100)
ADMIN_ID = int(os.getenv("ADMIN_ID", "1619432734"))             # кому слать уведомления

DATABASE_URL = os.getenv("DATABASE_URL", "")  # PostgreSQL на Render

# секрет в адресе вебхука
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me-please")

PORT = int(os.getenv("PORT", "10000"))

# ЮMoney кошелек: форма оплаты + HTTP-уведомления.
YOOMONEY_RECEIVER = os.getenv("YOOMONEY_RECEIVER", "").strip()  # номер кошелька 4100...
YOOMONEY_NOTIFICATION_SECRET = os.getenv("YOOMONEY_NOTIFICATION_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://adelin-miller.onrender.com").rstrip("/")

DEFAULT_PRICES = {
    "month": int(os.getenv("PRICE_MONTH", "699")),
    "forever": int(os.getenv("PRICE_FOREVER", "9990")),
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
    "На неё придёт чек об оплате. Просто отправь её сообщением."
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

# ---------------------------------------------------------------------------
# Вспомогательные функции цен
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Buy(StatesGroup):
    waiting_email = State()


router = Router()


def get_price(tariff_key: str) -> int:
    if not DATABASE_URL:
        return DEFAULT_PRICES.get(tariff_key, 699)
    try:
        conn = get_db()
        rows = conn.run("SELECT price FROM prices WHERE tariff=:tf", tf=tariff_key)
        conn.close()
        if rows:
            return int(rows[0][0])
    except Exception as e:
        log.error("Ошибка считывания цены %s из БД: %s", tariff_key, e)
    return DEFAULT_PRICES.get(tariff_key, 699)


def set_db_price(tariff_key: str, price: int):
    if not DATABASE_URL:
        return
    conn = get_db()
    conn.run(
        "INSERT INTO prices (tariff, price) VALUES (:tf, :pr) "
        "ON CONFLICT (tariff) DO UPDATE SET price=:pr",
        tf=tariff_key, pr=price
    )
    conn.close()


# ---------------------------------------------------------------------------
# База данных
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
        log.warning("DATABASE_URL не задан — учет работать НЕ будет.")
        return
    try:
        conn = get_db()
        conn.run('''CREATE TABLE IF NOT EXISTS members (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            tariff TEXT,
            start_dt TIMESTAMP,
            end_dt TIMESTAMP,
            status TEXT
        )''')
        try:
            conn.run("ALTER TABLE members ADD COLUMN IF NOT EXISTS reminded_stage INTEGER DEFAULT 0")
        except Exception as _e:
            log.warning("reminded_stage уже есть или не добавилась: %s", _e)
        
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
        conn.run('''CREATE TABLE IF NOT EXISTS prices (
            tariff TEXT PRIMARY KEY,
            price INTEGER
        )''')
        conn.close()
        log.info("База данных готова.")
    except Exception as e:
        log.error("Ошибка инициализации базы: %s", e)


def import_pending_from_json():
    if not DATABASE_URL:
        return
    path = os.getenv("IMPORT_FILE", "members_import.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.error("Не смог прочитать файл импорта %s: %s", path, e)
        return

    to_members = 0
    to_pending = 0
    try:
        conn = get_db()
        for m in data:
            uname = (m.get("username") or "").strip().lstrip("@")
            if not uname:
                continue
            tariff = m.get("tariff") or "month"
            uid = m.get("id")
            end_raw = m.get("end")
            end_dt = None
            if end_raw:
                try:
                    end_dt = datetime.strptime(end_raw, "%Y-%m-%d")
                except ValueError:
                    end_dt = None

            if uid:
                exists = conn.run(
                    "SELECT 1 FROM members WHERE user_id=:uid AND status='active'", uid=uid)
                if exists:
                    continue
                conn.run(
                    "INSERT INTO members (user_id, username, tariff, start_dt, end_dt, status, reminded_stage) "
                    "VALUES (:uid, :un, :tf, :sd, :ed, 'active', 0) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "username=:un, tariff=:tf, end_dt=:ed, status='active'",
                    uid=int(uid), un=uname, tf=tariff, sd=datetime.now(), ed=end_dt)
                to_members += 1
            else:
                in_main = conn.run(
                    "SELECT 1 FROM members WHERE LOWER(username)=LOWER(:un)", un=uname)
                if in_main:
                    continue
                conn.run(
                    "INSERT INTO pending_members (username, tariff, end_dt) "
                    "VALUES (:un, :tf, :ed) ON CONFLICT (username) DO UPDATE SET "
                    "tariff=:tf, end_dt=:ed",
                    un=uname, tf=tariff, ed=end_dt)
                to_pending += 1
        conn.close()
        log.info("Импорт завершен: в базу %s, в ожидание %s", to_members, to_pending)
    except Exception as e:
        log.error("Ошибка импорта: %s", e)


def save_consent(user_id: int, username: str):
    log.info("Согласие с офертой: user_id=%s username=%s", user_id, username)
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run(
            "INSERT INTO consents (user_id, username, accepted_at) VALUES (:uid, :un, :ts)",
            uid=user_id, un=username or "—", ts=datetime.now(),
        )
        conn.close()
    except Exception as e:
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
    except Exception as e:
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
    except Exception as e:
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
    except Exception as e:
        log.error("Не смог прочитать оплату ЮMoney %s: %s", label, e)
        return None


def mark_yoomoney_attempt_paid(label: str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run("UPDATE yoomoney_attempts SET status='paid' WHERE label=:label", label=label)
        conn.close()
    except Exception as e:
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
    except Exception as e:
        log.error("check_expired: ошибка: %s", e)
        return

    for user_id, end_dt, username in rows:
        if (end_dt - now).total_seconds() <= 0:
            kicked_ok = False
            try:
                await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
                await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
                kicked_ok = True
            except Exception as e:
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
                    except Exception:
                        pass
                    log.info("Истёк и убран тихо: @%s (%s)", username, user_id)
                    continue
                log.error("Ошибка кика %s: %s", user_id, e)
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ <b>Не удалось удалить участника</b>\n\n"
                        f"Пользователь: @{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Причина: {e}\n"
                        f"Удалить вручную: /remove {user_id}",
                    )
                except Exception:
                    pass
                continue

            try:
                c = get_db()
                c.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=user_id)
                c.close()
            except Exception:
                pass
            try:
                await bot.send_message(
                    user_id,
                    "Твоя подписка на месяц закончилась, доступ в клуб закрыт 🤍\n\n"
                    "Будем рады видеть снова — нажми /start, чтобы продлить.",
                )
            except Exception:
                pass
            log.info("Кикнут по истечении срока: %s", user_id)
            if kicked_ok:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚪 <b>Участник удалён по окончании подписки</b>\n\n"
                        f"Пользователь: @{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Подписка закончилась: {end_dt.strftime('%d.%m.%Y')}",
                    )
                except Exception as e2:
                    log.error("Не смог уведомить админа: %s", e2)


REMIND_3_DAYS = (
    "Привет 🤍 Через 3 дня твой доступ в Creator Lab заканчивается.\n\n"
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
    except Exception as e:
        log.error("check_reminders ошибка: %s", e)
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
            except Exception as e:
                log.error("Не смог отправить напоминание (1д) %s: %s", user_id, e)

        elif 1 < days_left <= 3 and reminded == 0:
            try:
                await bot.send_message(user_id, REMIND_3_DAYS, reply_markup=renew_keyboard())
                c = get_db()
                c.run("UPDATE members SET reminded_stage=3 WHERE user_id=:uid", uid=user_id)
                c.close()
            except Exception as e:
                log.error("Не смог отправить напоминание (3д) %s: %s", user_id, e)


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
    except Exception as e:
        return f"Не смог прочитать базу: {e}"

    forever_n = forever[0][0] if forever else 0
    expired_n = expired[0][0] if expired else 0

    lines = [
        "✅ <b>Я на посту и слежу за подписками</b>\n",
        f"👥 Активных месячных: {len(active)}",
        f"💎 Навсегда: {forever_n}",
        f"🚪 Уже удалённых: {expired_n}\n",
        f"💰 <b>Текущие цены:</b> 1 мес — {get_price('month')}₽ | Навсегда — {get_price('forever')}₽\n"
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
    except Exception as e:
        log.error("Не смог отправить ежедневный отчёт: %s", e)


# ---------------------------------------------------------------------------
# Клавиатуры и оферта
# ---------------------------------------------------------------------------

def tariff_keyboard() -> InlineKeyboardMarkup:
    month_price = get_price("month")
    forever_price = get_price("forever")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💛 1 месяц — {month_price}₽",
                callback_data="tariff_month")],
            [InlineKeyboardButton(
                text=f"💎 Навсегда — {forever_price}₽",
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
    "Это правила доступа к клубу Creator Lab. Все материалы защищены авторским правом. "
    "Нажимая «Принимаю условия», ты подтверждаешь, что прочитал(а) оферту и согласен(на) с ней."
)


def accept_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_oferta")],
        ]
    )


WARNING_TEXT = (
    "⚠️ <b>ВАЖНО. Прочти перед оплатой.</b>\n\n"
    "Все материалы Creator Lab — это моя интеллектуальная собственность.\n\n"
    "🚫 При обнаружении факта копирования, перепродажи или использования моих "
    "материалов доступ будет закрыт без возврата средств.\n\n"
    "Нажимая «Я соглашаюсь», ты подтверждаешь согласие с условиями."
)


def warning_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я соглашаюсь", callback_data="accept_warning")],
        ]
    )


# ---------------------------------------------------------------------------
# Вспомогательные функции Админки
# ---------------------------------------------------------------------------

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
            rows = conn.run("SELECT user_id, username FROM members WHERE LOWER(username)=LOWER(:un)", un=username)
        conn.close()
        if rows:
            return rows[0][0], rows[0][1]
    except Exception:
        pass
    return None, None


def _is_blacklisted(user_id: int, username: Optional[str]) -> bool:
    if not DATABASE_URL:
        return False
    uname = _clean_username(username or "") if username else None
    conn = get_db()
    try:
        if uname:
            rows = conn.run("SELECT 1 FROM blacklisted_users WHERE user_id=:uid OR LOWER(username)=LOWER(:un)", uid=user_id, un=uname)
        else:
            rows = conn.run("SELECT 1 FROM blacklisted_users WHERE user_id=:uid", uid=user_id)
        return len(rows) > 0
    finally:
        conn.close()


def _add_to_blacklist(user_id: Optional[int], username: Optional[str]):
    if not DATABASE_URL:
        raise RuntimeError("База не подключена.")
    uname = _clean_username(username or "") if username else None
    conn = get_db()
    try:
        existing = []
        if uname:
            existing = conn.run("SELECT id FROM blacklisted_users WHERE LOWER(username)=LOWER(:un) LIMIT 1", un=uname)
        if existing:
            conn.run("UPDATE blacklisted_users SET user_id=COALESCE(:uid, user_id), username=COALESCE(:un, username), added_at=:ts WHERE id=:bid", uid=user_id, un=uname, ts=datetime.now(), bid=existing[0][0])
        elif user_id is not None:
            conn.run("INSERT INTO blacklisted_users (user_id, username, added_at) VALUES (:uid, :un, :ts) ON CONFLICT (user_id) DO UPDATE SET username=COALESCE(EXCLUDED.username, blacklisted_users.username), added_at=EXCLUDED.added_at", uid=user_id, un=uname, ts=datetime.now())
        elif uname:
            conn.run("INSERT INTO blacklisted_users (user_id, username, added_at) VALUES (NULL, :un, :ts)", un=uname, ts=datetime.now())
    finally:
        conn.close()


def _remove_from_blacklist(user_id: Optional[int], username: Optional[str]) -> int:
    if not DATABASE_URL:
        raise RuntimeError("База не подключена.")
    uname = _clean_username(username or "") if username else None
    conn = get_db()
    try:
        if user_id is not None and uname:
            rows = conn.run("SELECT id FROM blacklisted_users WHERE user_id=:uid OR LOWER(username)=LOWER(:un)", uid=user_id, un=uname)
        elif user_id is not None:
            rows = conn.run("SELECT id FROM blacklisted_users WHERE user_id=:uid", uid=user_id)
        elif uname:
            rows = conn.run("SELECT id FROM blacklisted_users WHERE LOWER(username)=LOWER(:un)", un=uname)
        else:
            rows = []
        for row in rows:
            conn.run("DELETE FROM blacklisted_users WHERE id=:bid", bid=row[0])
        return len(rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ПОЛНЫЙ НАБОР АДМИН-КОМАНД
# ---------------------------------------------------------------------------

@router.message(Command("status"))
async def cmd_status(message: Message):
    if not _is_admin(message):
        return
    text = await build_status_text()
    await message.answer(text)


@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[1] not in ("month", "forever"):
        await message.answer(
            "⚙️ <b>Изменение цен на тарифы</b>\n\n"
            "Использование:\n"
            "<code>/setprice month 699</code> — закрепить цену на 1 месяц\n"
            "<code>/setprice forever 9990</code> — закрепить цену навсегда"
        )
        return
    tariff_key = parts[1]
    try:
        new_price = int(parts[2])
    except ValueError:
        await message.answer("Цена должна быть числом.")
        return
    
    set_db_price(tariff_key, new_price)
    t_name = "1 месяц" if tariff_key == "month" else "Навсегда"
    await message.answer(f"✅ Успешно! Новая цена для тарифа «{t_name}»: <b>{new_price}₽</b>")


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "<b>Изменение цен:</b>\n"
        "/setprice month 699 — изменить цену 1 месяца\n"
        "/setprice forever 9990 — изменить цену тарифа Навсегда\n\n"
        "<b>Просмотр:</b>\n"
        "/status — краткая сводка (на посту ли я)\n"
        "/members — полный список участников со сроками\n"
        "/find @username или /find id — найти человека\n"
        "/stats — статистика продаж и участников\n\n"
        "<b>Ручное управление:</b>\n"
        "/add @username 30 — добавить на 30 дней\n"
        "/add 6882319264 30 — добавить по id\n"
        "/add @username forever — добавить навсегда\n"
        "/extend @username 30 — продлить на 30 дней\n"
        "/remove @username — кикнуть из клуба и убрать из учёта\n"
        "/remove 6882319264 — кикнуть по id\n"
        "/ban @username или /ban id — добавить в ЧС\n"
        "/unban @username или /unban id — снять из ЧС\n\n"
        "<b>Рассылка:</b>\n"
        "/sale 599 — отправить спец. предложение на 1 месяц\n\n"
        "<b>VIP:</b>\n"
        "/vip @username или /vip id — сделать VIP (вечный доступ + иммунитет)\n"
        "/unvip @username — снять VIP-статус\n\n"
        "<b>Обслуживание базы:</b>\n"
        "/sync — сверить базу с группой и убрать выбывших\n"
        "/reimport — восстановить из файла импорта\n"
        "/cleanup — показать просроченных\n"
        "/purge — разом убрать ВСЕХ просроченных"
    )


@router.message(Command("members"))
async def cmd_members(message: Message, bot: Bot):
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
            "ORDER BY status, end_dt NULLS LAST"
        )
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка чтения базы: {e}")
        return

    if not rows:
        await message.answer("В базе пока нет участников.")
        return

    active_month, vip, expired = [], [], []
    for username, tariff, start_dt, end_dt, status in rows:
        if status == "expired":
            expired.append((username, end_dt))
        elif end_dt is None or tariff == "vip":
            vip.append((username, start_dt))
        else:
            active_month.append((username, start_dt, end_dt))

    living = len(active_month) + len(vip)

    try:
        group_count = await bot.get_chat_member_count(CLUB_CHAT_ID)
        group_line = f"👤 Сейчас в группе (по Telegram): {group_count}\n"
    except Exception:
        group_line = ""

    lines = [f"👥 <b>Участников по базе (активных): {living}</b>", group_line]

    if active_month:
        lines.append(f"\n💛 <b>По подписке ({len(active_month)}):</b>")
        for i, (username, start_dt, end_dt) in enumerate(active_month, 1):
            days_left = int((end_dt - now).total_seconds() / 86400)
            mark = "⚠️" if days_left <= 3 else "✅"
            lines.append(f"{i}. {mark} @{username or '—'} — до {end_dt.strftime('%d.%m.%Y')} (ост. {days_left} дн.)")

    if vip:
        lines.append(f"\n💎 <b>Навсегда / VIP ({len(vip)}):</b>")
        for i, (username, start_dt) in enumerate(vip, 1):
            lines.append(f"{i}. 💎 @{username or '—'}")

    if expired:
        lines.append(f"\n🗄 В архиве (истёкшие, скрыты): {len(expired)}")

    chunk = []
    for line in lines:
        chunk.append(line)
        if len(chunk) >= 50:
            await message.answer("\n".join(chunk))
            chunk = []
    if chunk:
        await message.answer("\n".join(chunk))


@router.message(Command("find"))
async def cmd_find(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /find @username или /find 6882319264")
        return
    uid_in, uname = _parse_target(parts[1])
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return
    now = datetime.now()
    try:
        conn = get_db()
        if uid_in is not None:
            rows = conn.run("SELECT user_id, username, tariff, start_dt, end_dt, status FROM members WHERE user_id=:uid", uid=uid_in)
        else:
            rows = conn.run("SELECT user_id, username, tariff, start_dt, end_dt, status FROM members WHERE LOWER(username)=LOWER(:un)", un=uname)
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    if not rows:
        who = f"id {uid_in}" if uid_in is not None else f"@{uname}"
        await message.answer(f"{who} не найден в базе.")
        return

    out = []
    for user_id, username, tariff, start_dt, end_dt, status in rows:
        s = start_dt.strftime('%d.%m.%Y') if start_dt else "—"
        if end_dt is None:
            period = "навсегда 💎"
        else:
            days_left = int((end_dt - now).total_seconds() / 86400)
            period = f"{s} → {end_dt.strftime('%d.%m.%Y')} (осталось {days_left} дн.)"
        out.append(
            f"👤 @{username or '—'} (id <code>{user_id}</code>)\n"
            f"Тариф: {tariff}\nСтатус: {status}\nСрок: {period}"
        )
    await message.answer("\n\n".join(out))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not _is_admin(message):
        return
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return
    try:
        conn = get_db()
        total = conn.run("SELECT COUNT(*) FROM members")[0][0]
        active_month = conn.run("SELECT COUNT(*) FROM members WHERE status='active' AND end_dt IS NOT NULL")[0][0]
        forever = conn.run("SELECT COUNT(*) FROM members WHERE status='active' AND end_dt IS NULL")[0][0]
        expired = conn.run("SELECT COUNT(*) FROM members WHERE status='expired'")[0][0]
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return
    await message.answer(
        "📊 <b>Статистика клуба</b>\n\n"
        f"Всего записей: {total}\n"
        f"✅ Активных (месяц): {active_month}\n"
        f"💎 Навсегда: {forever}\n"
        f"❌ Истёкших: {expired}\n\n"
        f"Сейчас в клубе активны: {active_month + forever}"
    )


@router.message(Command("sync"))
async def cmd_sync(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return

    await message.answer("🔄 Сверяю базу с группой, это займёт минуту…")
    try:
        conn = get_db()
        rows = conn.run("SELECT user_id, username, tariff FROM members WHERE status='active'")
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка чтения базы: {e}")
        return

    ghosts, checked, errors = [], 0, 0
    for user_id, username, tariff in rows:
        checked += 1
        try:
            member = await bot.get_chat_member(CLUB_CHAT_ID, user_id)
            if member.status in ("left", "kicked"):
                ghosts.append((user_id, username, tariff))
        except Exception as e:
            errors += 1
            log.warning("sync: не смог проверить @%s (id %s): %s", username, user_id, e)
        await asyncio.sleep(0.05)

    if not ghosts:
        msg = f"✅ Готово! Проверено {checked}, все на месте. Призраков нет 🎉"
        if errors:
            msg += f"\n\n(⚠️ {errors} не смог проверить — их не трогал)"
        await message.answer(msg)
        return

    try:
        conn = get_db()
        for user_id, _u, _t in ghosts:
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=user_id)
        conn.close()
    except Exception as e:
        await message.answer(f"Нашёл призраков, но не смог обновить базу: {e}")
        return

    lines = [f"🧹 <b>Синхронизация завершена</b>\n", f"Проверено: {checked}", f"Убрано призраков: {len(ghosts)}\n", "<b>Кого убрал из учёта:</b>"]
    for i, (user_id, username, tariff) in enumerate(ghosts, 1):
        tag = "💎VIP" if tariff == "vip" else "месяц"
        lines.append(f"{i}. @{username or '—'} ({tag})")

    chunk = []
    for line in lines:
        chunk.append(line)
        if len(chunk) >= 50:
            await message.answer("\n".join(chunk))
            chunk = []
    if chunk:
        await message.answer("\n".join(chunk))


@router.message(Command("reimport"))
async def cmd_reimport(message: Message):
    if not _is_admin(message):
        return
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return
    path = os.getenv("IMPORT_FILE", "members_import.json")
    if not os.path.exists(path):
        await message.answer(f"Файл {path} не найден.")
        return
    await message.answer("🔄 Перечитываю список из файла и восстанавливаю базу…")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        await message.answer(f"Не смог прочитать файл: {e}")
        return

    restored = 0
    try:
        conn = get_db()
        for m in data:
            uname = (m.get("username") or "").strip().lstrip("@")
            uid = m.get("id")
            if not uid:
                continue
            tariff = m.get("tariff") or "month"
            end_raw = m.get("end")
            end_dt = None
            if end_raw:
                try:
                    end_dt = datetime.strptime(end_raw, "%Y-%m-%d")
                except ValueError:
                    end_dt = None
            conn.run(
                "INSERT INTO members (user_id, username, tariff, start_dt, end_dt, status, reminded_stage) "
                "VALUES (:uid, :un, :tf, :sd, :ed, 'active', 0) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "username=:un, tariff=:tf, end_dt=:ed, status='active'",
                uid=int(uid), un=uname, tf=tariff, sd=datetime.now(), ed=end_dt)
            restored += 1
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка восстановления: {e}")
        return
    await message.answer(f"✅ Восстановлено {restored} участников из файла.")


@router.message(Command("sale"))
async def cmd_sale(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /sale 599")
        return
    try:
        sale_price = int(parts[1].strip())
    except ValueError:
        await message.answer("Цена должна быть числом. Например: /sale 599")
        return
    if sale_price <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return

    try:
        conn = get_db()
        rows = conn.run(
            "SELECT DISTINCT ON (c.user_id) c.user_id, NULLIF(NULLIF(c.username, '—'), '') "
            "FROM consents c "
            "LEFT JOIN members m ON m.user_id=c.user_id AND m.status='active' "
            "LEFT JOIN blacklisted_users b ON b.user_id=c.user_id "
            "WHERE c.user_id IS NOT NULL AND m.user_id IS NULL AND b.id IS NULL "
            "ORDER BY c.user_id, c.accepted_at DESC"
        )
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка чтения базы: {e}")
        return

    if not rows:
        await message.answer("Некому отправлять рассылку.")
        return

    await message.answer(f"Начинаю рассылку предложения за {sale_price}₽ ({len(rows)} чел.).")

    sale_text = (
        "Привет! Сейчас можно попасть в Creator Lab со скидкой 💛\n\n"
        f"1 месяц доступа — {sale_price}₽.\n"
        "Внутри клуба материалы, промпты, сервисы и поддержка.\n\n"
        "Нажми кнопку ниже, чтобы забрать доступ со скидкой."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"Забрать доступ за {sale_price}₽",
                callback_data=f"sale_month:{sale_price}",
            )
        ]]
    )

    sent, failed = 0, 0
    for user_id, _username in rows:
        try:
            await bot.send_message(user_id, sale_text, reply_markup=kb)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(f"✅ Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}")


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 and not message.forward_from:
        await message.answer("Использование: /ban @username или /ban 6882319264")
        return

    uid, uname = None, None
    if len(parts) >= 2:
        uid, uname = _parse_target(parts[1])
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname
    if uid is None and uname:
        found_uid, found_uname = await _lookup_in_db(username=uname)
        if found_uid:
            uid, uname = found_uid, found_uname

    try:
        _add_to_blacklist(uid, uname)
    except Exception as e:
        await message.answer(f"Ошибка добавления в ЧС: {e}")
        return

    name_show = ("@" + uname) if uname else f"id {uid}"
    await message.answer(f"🚫 {name_show} добавлен(а) в черный список.")


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 and not message.forward_from:
        await message.answer("Использование: /unban @username или /unban 6882319264")
        return

    uid, uname = None, None
    if len(parts) >= 2:
        uid, uname = _parse_target(parts[1])
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname
    if uid is None and uname:
        found_uid, found_uname = await _lookup_in_db(username=uname)
        if found_uid:
            uid, uname = found_uid, found_uname

    try:
        removed = _remove_from_blacklist(uid, uname)
    except Exception as e:
        await message.answer(f"Ошибка снятия с ЧС: {e}")
        return

    name_show = ("@" + uname) if uname else f"id {uid}"
    if removed:
        await message.answer(f"✅ {name_show} снят(а) с черного списка.")
    else:
        await message.answer(f"{name_show} не найден(а) в черном списке.")


@router.message(Command("add"))
async def cmd_add(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /add @username 30 или /add @username forever")
        return
    target, term = parts[1], parts[2].lower()

    uid, uname = _parse_target(target)
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname

    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer(f"Не знаю id для @{uname}. Укажи числовой id.")
        return

    start_dt = datetime.now()
    if term == "forever":
        end_dt = None
        tariff = "forever"
    elif "." in term:
        end_dt = None
        for fmt in ("%d.%m.%Y", "%d.%m.%y"):
            try:
                end_dt = datetime.strptime(term, fmt)
                break
            except ValueError:
                continue
        if end_dt is None:
            await message.answer("Дату пиши так: 16.07.2026.")
            return
        tariff = "month"
    else:
        try:
            days = int(term)
        except ValueError:
            await message.answer("Срок: число дней, дата или forever.")
            return
        end_dt = start_dt + timedelta(days=days)
        tariff = "month"

    name_show = ("@" + uname) if uname else f"id {uid}"
    save_member(uid, uname or "—", tariff, start_dt, end_dt)
    if end_dt:
        await message.answer(f"✅ Добавлен(а) {name_show} на {term} дн. (до {end_dt.strftime('%d.%m.%Y')})")
    else:
        await message.answer(f"✅ Добавлен(а) {name_show} навсегда 💎")


@router.message(Command("vip"))
async def cmd_vip(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /vip @username или /vip 6882319264")
        return
    uid, uname = _parse_target(parts[1])
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname
    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer(f"Не знаю id для @{uname}.")
        return

    save_member(uid, uname or "—", "vip", datetime.now(), None)
    name_show = ("@" + uname) if uname else f"id {uid}"
    await message.answer(f"👑 {name_show} теперь VIP (вечный доступ)!")


@router.message(Command("unvip"))
async def cmd_unvip(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /unvip @username")
        return
    uid, uname = _parse_target(parts[1])
    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer(f"Не нашёл @{uname} в базе.")
        return
    if DATABASE_URL:
        try:
            conn = get_db()
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid AND tariff='vip'", uid=uid)
            conn.close()
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
            return
    name_show = ("@" + uname) if uname else f"id {uid}"
    await message.answer(f"VIP-статус снят с {name_show}.")


@router.message(Command("extend"))
async def cmd_extend(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Использование: /extend @username 30")
        return
    uid_in, uname = _parse_target(parts[1])
    try:
        days = int(parts[2])
    except ValueError:
        await message.answer("Срок должен быть числом дней.")
        return
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return
    now = datetime.now()
    try:
        conn = get_db()
        if uid_in is not None:
            rows = conn.run("SELECT user_id, end_dt, username FROM members WHERE user_id=:uid", uid=uid_in)
        else:
            rows = conn.run("SELECT user_id, end_dt, username FROM members WHERE LOWER(username)=LOWER(:un)", un=uname)
        if not rows:
            conn.close()
            await message.answer("Пользователь не найден.")
            return
        user_id, end_dt, db_name = rows[0]
        base = end_dt if (end_dt and end_dt > now) else now
        new_end = base + timedelta(days=days)
        conn.run("UPDATE members SET end_dt=:ed, status='active', reminded_stage=0 WHERE user_id=:uid", ed=new_end, uid=user_id)
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return
    name_show = ("@" + db_name) if db_name else f"id {user_id}"
    await message.answer(f"✅ Продлено {name_show} на {days} дн. До: {new_end.strftime('%d.%m.%Y')}")


@router.message(Command("remove"))
async def cmd_remove(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /remove @username или /remove 6882319264")
        return

    uid, uname = _parse_target(parts[1])
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname

    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer(f"Не нашёл @{uname} в базе. Укажи ID.")
        return

    if DATABASE_URL:
        try:
            conn = get_db()
            vip = conn.run("SELECT tariff FROM members WHERE user_id=:uid AND tariff='vip' AND status='active'", uid=uid)
            conn.close()
            if vip:
                await message.answer("👑 Это VIP-участник. Сначала сними VIP (/unvip).")
                return
        except Exception:
            pass

    if DATABASE_URL:
        try:
            conn = get_db()
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=uid)
            conn.close()
        except Exception as e:
            log.error("remove: ошибка базы для %s: %s", uid, e)

    name_show = ("@" + uname) if uname else f"id {uid}"
    try:
        await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await message.answer(f"✅ {name_show} удалён(а) из клуба.")
    except Exception as e:
        await message.answer(f"⚠️ {name_show}: из учёта убран(а), но кикнуть из группы не вышло: {e}")


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    if not _is_admin(message):
        return
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return
    now = datetime.now()
    try:
        conn = get_db()
        rows = conn.run(
            "SELECT user_id, username, end_dt FROM members "
            "WHERE status='active' AND end_dt IS NOT NULL AND end_dt < :now "
            "ORDER BY end_dt", now=now)
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    if not rows:
        await message.answer("✅ Должников нет — все активные в сроке 🎉")
        return

    await message.answer(f"⏰ <b>У этих закончился срок ({len(rows)}):</b>")
    for user_id, username, end_dt in rows:
        d = end_dt.strftime("%d.%m.%Y") if end_dt else "—"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Убрать", callback_data=f"kick:{user_id}"),
            InlineKeyboardButton(text="💛 +30 дней", callback_data=f"ext30:{user_id}"),
        ]])
        await message.answer(f"@{username or '—'} (id <code>{user_id}</code>) — истёк {d}", reply_markup=kb)


@router.message(Command("purge"))
async def cmd_purge(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return
    now = datetime.now()
    try:
        conn = get_db()
        rows = conn.run("SELECT user_id, username FROM members WHERE status='active' AND end_dt IS NOT NULL AND end_dt < :now", now=now)
        conn.close()
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    if not rows:
        await message.answer("✅ Просроченных нет.")
        return

    await message.answer(f"🧹 Убираю {len(rows)} просроченных…")
    kicked, archived = 0, 0
    for user_id, username in rows:
        try:
            conn = get_db()
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=user_id)
            conn.close()
            archived += 1
        except Exception:
            pass
        try:
            await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
            await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
            kicked += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)

    await message.answer(f"✅ Готово!\nУбрано из учёта: {archived}\nКикнуто из клуба: {kicked}")


@router.callback_query(F.data.startswith("kick:"))
async def on_kick_button(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа")
        return
    uid = int(cb.data.split(":")[1])
    if DATABASE_URL:
        try:
            conn = get_db()
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=uid)
            conn.close()
        except Exception:
            pass
    try:
        await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        note = "удалён из клуба и убран из учёта"
    except Exception:
        note = "убран из учёта (в группе его не было)"
    await cb.message.edit_text(f"✅ {note}.")
    await cb.answer("Готово")


@router.callback_query(F.data.startswith("ext30:"))
async def on_ext30_button(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа")
        return
    uid = int(cb.data.split(":")[1])
    now = datetime.now()
    new_end = now + timedelta(days=30)
    if DATABASE_URL:
        try:
            conn = get_db()
            conn.run("UPDATE members SET end_dt=:ed, status='active', reminded_stage=0 WHERE user_id=:uid", ed=new_end, uid=uid)
            conn.close()
        except Exception:
            pass
    await cb.message.edit_text(f"💛 Продлён на 30 дней — до {new_end.strftime('%d.%m.%Y')}.")
    await cb.answer("Продлено")


# ---------------------------------------------------------------------------
# Сценарий покупки для пользователя
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    try:
        await message.answer_document(
            FSInputFile(OFERTA_PATH, filename="Договор-оферта Creator Lab.pdf"),
            caption=OFERTA_TEXT,
            reply_markup=accept_keyboard(),
        )
    except Exception as e:
        log.error("Не смог отправить PDF: %s", e)
        await message.answer(OFERTA_TEXT, reply_markup=accept_keyboard())


@router.callback_query(F.data == "accept_oferta")
async def on_accept(cb: CallbackQuery, state: FSMContext):
    user = cb.from_user
    save_consent(user.id, user.username)
    await cb.message.answer(WARNING_TEXT, reply_markup=warning_keyboard())
    await cb.answer("Принято ✅")


@router.callback_query(F.data == "accept_warning")
async def on_accept_warning(cb: CallbackQuery, state: FSMContext):
    user = cb.from_user
    save_consent(user.id, user.username)
    await cb.message.answer(GREETING, reply_markup=tariff_keyboard())
    await cb.answer("Принято ✅")


@router.callback_query(F.data.startswith("sale_month:"))
async def on_sale_month(cb: CallbackQuery, state: FSMContext):
    user = cb.from_user
    try:
        sale_price = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.message.answer("Ссылка устарела. Нажми /start и выбери тариф заново.")
        await cb.answer()
        return
    if not user.username:
        await cb.message.answer(ASK_USERNAME)
        await cb.answer()
        return
    if _is_blacklisted(user.id, user.username):
        await cb.message.answer(BLACKLIST_NOTICE)
        await cb.answer()
        return

    await state.update_data(tariff="month", sale_price=sale_price)
    await state.set_state(Buy.waiting_email)
    await cb.message.answer(f"Отлично, оформляем доступ на 1 месяц за {sale_price}₽.\n\n" + ASK_EMAIL)
    await cb.answer()


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
    
    t_name = "1 месяц" if tariff_key == "month" else "Навсегда"
    await cb.message.answer(f"Тариф «{t_name}» — отличный выбор 🙂\n\n" + ASK_EMAIL)
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
    await cb.message.answer("Отлично, продлеваем доступ на месяц 💛\n\n" + ASK_EMAIL)
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
    tariff_key = data.get("tariff", "month")
    sale_price = data.get("sale_price")
    
    if not YOOMONEY_RECEIVER:
        await message.answer("Оплата через ЮMoney временно недоступна. Свяжитесь с @adelin_creator.")
        await state.clear()
        return

    price = int(sale_price) if sale_price else get_price(tariff_key)
    label = f"ym_{user.id}_{uuid.uuid4().hex[:12]}"
    
    save_yoomoney_attempt(label, email, user.id, user.username, tariff_key, price)
    await state.clear()

    payment_url = f"{PUBLIC_BASE_URL}/yoomoney/pay/{label}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате 💳", url=payment_url)]]
    )

    t_name = "1 месяц" if tariff_key == "month" else "Навсегда"
    await message.answer(
        f"Готово! Оформление тарифа «{t_name}».\n"
        f"Сумма к оплате: <b>{price}₽</b>\n\n"
        "Нажми на кнопку ниже для перехода к оплате ЮMoney (карта/СБП/кошелек).\n"
        "Как только оплата пройдет, бот моментально пришлет тебе доступ!",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# Выдача доступа
# ---------------------------------------------------------------------------

async def grant_access(bot: Bot, email: str, amount, currency, contract_id: str,
                       tariff_name: str = "—", tariff_key: Optional[str] = None,
                       user_id: Optional[int] = None, username: Optional[str] = None):
    username = username or "—"

    if _is_blacklisted(user_id, username):
        try:
            await bot.send_message(user_id, BLACKLIST_NOTICE)
        except Exception:
            pass
        await bot.send_message(
            ADMIN_ID,
            f"🚫 Оплата от @{username} (id <code>{user_id}</code>, {email}) пришла, "
            f"но пользователь находится в ЧС.",
        )
        return

    try:
        link = await bot.create_chat_invite_link(
            chat_id=CLUB_CHAT_ID,
            member_limit=1,
            name=f"buyer {user_id}",
        )
        invite_url = link.invite_link
    except Exception as e:
        log.exception("Не смог создать ссылку-приглашение: %s", e)
        await bot.send_message(
            ADMIN_ID,
            f"❗️ Оплата прошла (@{username}, {email}), но я не смог создать ссылку в клуб: {e}",
        )
        return

    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=30) if tariff_key == "month" else None

    if end_dt:
        period_line = (
            f"📅 Твой доступ активен с {start_dt.strftime('%d.%m.%Y')} "
            f"по {end_dt.strftime('%d.%m.%Y')}.\n"
            "Я заранее напомню тебе об окончании подписки 🤍\n\n"
        )
    else:
        period_line = "📅 Твой доступ — навсегда, без ограничения по сроку 💎\n\n"

    try:
        await bot.send_message(
            user_id,
            "Оплата получена, спасибо! 💛\n\n"
            "Добро пожаловать в Creator Lab 🔐\n\n"
            f"{period_line}"
            "Вот твоя личная ссылка в клуб:\n"
            f"{invite_url}\n\n"
            "Заходи и пользуйся 🚀",
        )
    except Exception as e:
        log.exception("Не смог отправить ссылку покупателю: %s", e)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ <b>Оплата прошла, но пользователь заблокировал бота</b>\n\n"
            f"Покупатель: @{username} (id <code>{user_id}</code>)\n"
            f"Ссылка для отправки вручную:\n{invite_url}",
        )
        return

    save_member(user_id, username, tariff_key or "—", start_dt, end_dt)

    access_str = f"с {start_dt.strftime('%d.%m.%Y')} по {end_dt.strftime('%d.%m.%Y')}" if end_dt else "бессрочно"
    await bot.send_message(
        ADMIN_ID,
        f"💰 <b>Новая продажа через ЮMoney!</b>\n\n"
        f"Тариф: {tariff_name}\n"
        f"Покупатель: @{username} (id <code>{user_id}</code>)\n"
        f"Почта: {email}\n"
        f"Сумма: {amount} {currency}\n"
        f"Доступ: {access_str}",
    )


# ---------------------------------------------------------------------------
# Вебхук ЮMoney
# ---------------------------------------------------------------------------

def verify_yoomoney_sign(params: dict[str, str]) -> bool:
    if not YOOMONEY_NOTIFICATION_SECRET:
        log.error("YOOMONEY_NOTIFICATION_SECRET не задан")
        return False
    given = (params.get("sha1_hash") or "").lower()
    if not given:
        return False

    str_to_hash = f"{params.get('notification_type')}&{params.get('operation_id')}&{params.get('amount')}&{params.get('currency')}&{params.get('datetime')}&{params.get('sender')}&{params.get('codepro')}&{YOOMONEY_NOTIFICATION_SECRET}&{params.get('label')}"
    
    digest = hashlib.sha1(str_to_hash.encode('utf-8')).hexdigest()
    return hmac.compare_digest(digest.lower(), given)


async def handle_yoomoney_pay(request: web.Request) -> web.Response:
    label = request.match_info.get("label", "")
    info = load_yoomoney_attempt(label)
    if not info or info.get("status") == "paid":
        return web.Response(status=404, text="Платеж не найден или уже оплачен")
    if not YOOMONEY_RECEIVER:
        return web.Response(status=500, text="Кошелек ЮMoney не настроен")

    tariff_key = info.get("tariff")
    tariff_name = "1 месяц" if tariff_key == "month" else "Навсегда"
    amount = int(info.get("amount") or get_price(tariff_key))
    email = info.get("email") or ""
    targets = f"Creator Lab: {tariff_name}"

    fields = {
        "receiver": YOOMONEY_RECEIVER,
        "quickpay-form": "shop",
        "paymentType": "SB",
        "sum": str(amount),
        "label": label,
        "targets": targets,
        "successURL": f"https://t.me/adelin_creator",
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
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Переход к оплате ЮMoney...</title>
</head>
<body>
  <p>Загрузка формы оплаты ЮMoney...</p>
  <form id="pay" method="POST" action="https://yoomoney.ru/quickpay/confirm">
    {inputs}
  </form>
  <script>document.getElementById('pay').submit();</script>
</body>
</html>"""
    return web.Response(text=page, content_type="text/html")


async def handle_yoomoney_notification(request: web.Request) -> web.Response:
    if request.match_info.get("secret") != WEBHOOK_SECRET:
        return web.Response(status=404, text="not found")

    form = await request.post()
    params = {k: str(v) for k, v in form.items()}
    log.info("Уведомление от ЮMoney: %s", json.dumps(params, ensure_ascii=False))

    if not verify_yoomoney_sign(params):
        log.warning("ЮMoney: неверная подпись SHA1")
        return web.Response(status=403, text="bad sign")

    if params.get("unaccepted") == "true" or params.get("codepro") == "true":
        return web.Response(text="ignored")

    label = params.get("label") or ""
    info = load_yoomoney_attempt(label)
    if not info:
        log.warning("ЮMoney: неизвестный label %s", label)
        return web.Response(text="ok")

    if info.get("status") == "paid":
        return web.Response(text="duplicate")

    expected = int(info.get("amount") or 0)
    paid_raw = params.get("withdraw_amount") or params.get("amount") or "0"
    try:
        paid = float(str(paid_raw).replace(",", "."))
    except ValueError:
        paid = 0

    if paid + 0.01 < expected:
        log.warning("Сумма меньше ожидаемой: %s < %s", paid, expected)
        return web.Response(text="ignored")

    tariff_key = info.get("tariff")
    tariff_name = "1 месяц" if tariff_key == "month" else "Навсегда"

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
    return web.Response(text="ok")


async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

@router.message(F.chat.id == CLUB_CHAT_ID)
async def collect_id_from_group(message: Message):
    if not DATABASE_URL or not message.from_user:
        return
    user = message.from_user
    if not user.username:
        return
    uname = user.username
    try:
        conn = get_db()
        in_main = conn.run("SELECT 1 FROM members WHERE user_id=:uid", uid=user.id)
        if in_main:
            conn.run("UPDATE members SET username=:un WHERE user_id=:uid", un=uname, uid=user.id)
            conn.close()
            return
        pend = conn.run("SELECT tariff, end_dt FROM pending_members WHERE LOWER(username)=LOWER(:un)", un=uname)
        if pend:
            tariff, end_dt = pend[0]
            conn.run(
                "INSERT INTO members (user_id, username, tariff, start_dt, end_dt, status, reminded_stage) "
                "VALUES (:uid, :un, :tf, :sd, :ed, 'active', 0) "
                "ON CONFLICT (user_id) DO UPDATE SET username=:un, tariff=:tf, end_dt=:ed, status='active'",
                uid=user.id, un=uname, tf=tariff, sd=datetime.now(), ed=end_dt)
            conn.run("DELETE FROM pending_members WHERE LOWER(username)=LOWER(:un)", un=uname)
            log.info("Из ожидания в базу: @%s id=%s (%s)", uname, user.id, tariff)
        conn.close()
    except Exception as e:
        log.error("collect_id_from_group ошибка для @%s: %s", uname, e)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    init_db()
    import_pending_from_json()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expired, "interval", hours=1, args=[bot])
    scheduler.add_job(check_reminders, "interval", hours=12, args=[bot])
    scheduler.add_job(daily_report, CronTrigger(hour=20, minute=0), args=[bot])
    scheduler.start()

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    
    app.router.add_get("/yoomoney/pay/{label}", handle_yoomoney_pay)
    app.router.add_post("/yoomoney/notification/{secret}", handle_yoomoney_notification)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    log.info("Сервер запущен на порту %s", PORT)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
