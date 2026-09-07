# -*- coding: utf-8 -*-
"""
Creator Lab access bot.

Оплата: ЮMoney.
Доступ: одноразовая персональная ссылка в закрытый Telegram-клуб.
База: PostgreSQL.
Фреймворк: aiogram 3 + aiohttp.

Ключевые правила:
1. Платёж считается действительным только после проверки подписи ЮMoney.
2. Для новых уведомлений используется sign = HMAC-SHA256 по документации ЮMoney.
3. Одна продажа хранится по label и безопасно переживает повторную доставку webhook.
4. Админ получает уведомление о продаже независимо от результата доставки пользователю.
5. /sync сверяет записи базы с фактическим членством через getChatMember.
6. Telegram Bot API не позволяет получить список всех участников группы. Поэтому
   синхронизация может проверить только тех пользователей, которые уже известны базе.
   Общее число участников берём через getChatMemberCount.
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
from urllib.parse import quote, unquote, urlsplit

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
    ChatMemberUpdated,
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

# ЮMoney
YOOMONEY_RECEIVER = os.getenv("YOOMONEY_RECEIVER", "").strip()
# Это секретный ключ HTTP-уведомлений ЮMoney.
# Он НЕ должен быть просто секретом в URL маршрута. Именно этот ключ
# участвует в HMAC-SHA256 расчёте параметра sign.
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

OFERTA_PATH = os.getenv("OFERTA_PATH", "oferta.pdf")


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


class AdminPrice(StatesGroup):
    waiting_for_price = State()


router = Router()


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def parse_db_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL должен начинаться с postgres:// или postgresql://")
    if not parsed.hostname or not parsed.path:
        raise ValueError("Некорректный DATABASE_URL")
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname
    port = parsed.port or 5432
    dbname = parsed.path.lstrip("/")
    return user, password, host, int(port), dbname


def get_db():
    user, password, host, port, dbname = parse_db_url(DATABASE_URL)
    return pg8000.native.Connection(
        user=user,
        password=password,
        host=host,
        port=port,
        database=dbname,
    )


def _set_db_setting(key: str, value: str):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            """INSERT INTO bot_settings (key, value) VALUES (:key, :value)
               ON CONFLICT (key) DO UPDATE SET value=:value""",
            key=key,
            value=value,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог сохранить настройку %s: %s", key, e)
    finally:
        if conn:
            conn.close()


def _get_db_setting(key: str) -> Optional[str]:
    if not DATABASE_URL:
        return None
    conn = None
    try:
        conn = get_db()
        rows = conn.run("SELECT value FROM bot_settings WHERE key=:key", key=key)
        return str(rows[0][0]) if rows else None
    except Exception as e:  # noqa: BLE001
        log.error("Не смог прочитать настройку %s: %s", key, e)
        return None
    finally:
        if conn:
            conn.close()


def load_prices_from_db():
    if not DATABASE_URL:
        return
    for key in ("month", "forever"):
        setting_key = f"price_{key}"
        saved = _get_db_setting(setting_key)
        if saved and saved.isdigit() and int(saved) > 0:
            TARIFFS[key]["price"] = int(saved)
        else:
            _set_db_setting(setting_key, str(TARIFFS[key]["price"]))


def init_db():
    if not DATABASE_URL:
        log.warning("DATABASE_URL не задан — база клуба отключена.")
        return

    conn = None
    try:
        conn = get_db()

        conn.run(
            """CREATE TABLE IF NOT EXISTS members (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                tariff TEXT,
                start_dt TIMESTAMP,
                end_dt TIMESTAMP,
                status TEXT,
                reminded_stage INTEGER DEFAULT 0,
                in_chat BOOLEAN DEFAULT FALSE,
                chat_status TEXT,
                last_synced_at TIMESTAMP
            )"""
        )

        conn.run(
            """CREATE TABLE IF NOT EXISTS consents (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                accepted_at TIMESTAMP
            )"""
        )

        conn.run(
            """CREATE TABLE IF NOT EXISTS pending_members (
                username TEXT PRIMARY KEY,
                tariff TEXT,
                end_dt TIMESTAMP
            )"""
        )

        conn.run(
            """CREATE TABLE IF NOT EXISTS blacklisted_users (
                id SERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE,
                username TEXT,
                added_at TIMESTAMP
            )"""
        )

        conn.run(
            """CREATE TABLE IF NOT EXISTS yoomoney_attempts (
                label TEXT PRIMARY KEY,
                email TEXT,
                user_id BIGINT,
                username TEXT,
                tariff TEXT,
                amount INTEGER,
                created_at TIMESTAMP,
                status TEXT DEFAULT 'pending',
                operation_id TEXT,
                paid_amount NUMERIC,
                paid_at TIMESTAMP,
                invite_link TEXT,
                access_start_dt TIMESTAMP,
                access_end_dt TIMESTAMP,
                access_granted BOOLEAN DEFAULT FALSE,
                user_notified BOOLEAN DEFAULT FALSE,
                admin_notified BOOLEAN DEFAULT FALSE
            )"""
        )

        conn.run(
            """CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )

        migrations = (
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS in_chat BOOLEAN DEFAULT FALSE",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS chat_status TEXT",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS operation_id TEXT",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS paid_amount NUMERIC",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS invite_link TEXT",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS access_start_dt TIMESTAMP",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS access_end_dt TIMESTAMP",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS access_granted BOOLEAN DEFAULT FALSE",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS user_notified BOOLEAN DEFAULT FALSE",
            "ALTER TABLE yoomoney_attempts ADD COLUMN IF NOT EXISTS admin_notified BOOLEAN DEFAULT FALSE",
        )
        for sql in migrations:
            conn.run(sql)

        conn.run("CREATE INDEX IF NOT EXISTS idx_members_status ON members(status)")
        conn.run("CREATE INDEX IF NOT EXISTS idx_members_username_lower ON members(LOWER(username))")

        log.info("База данных готова к работе.")
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка инициализации базы: %s", e)
    finally:
        if conn:
            conn.close()

    load_prices_from_db()


def save_consent(user_id: int, username: Optional[str]):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            "INSERT INTO consents (user_id, username, accepted_at) VALUES (:uid, :un, :ts)",
            uid=user_id,
            un=username or "—",
            ts=datetime.now(),
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог записать согласие %s: %s", user_id, e)
    finally:
        if conn:
            conn.close()


def get_member_record(user_id: int) -> Optional[dict]:
    if not DATABASE_URL:
        return None
    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT user_id, username, tariff, start_dt, end_dt, status,
                      reminded_stage, in_chat, chat_status, last_synced_at
               FROM members WHERE user_id=:uid""",
            uid=user_id,
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "user_id": row[0],
            "username": row[1],
            "tariff": row[2],
            "start_dt": row[3],
            "end_dt": row[4],
            "status": row[5],
            "reminded_stage": row[6] or 0,
            "in_chat": bool(row[7]),
            "chat_status": row[8],
            "last_synced_at": row[9],
        }
    except Exception as e:  # noqa: BLE001
        log.error("Не смог прочитать участника %s: %s", user_id, e)
        return None
    finally:
        if conn:
            conn.close()


def save_member(
    user_id: int,
    username: str,
    tariff_key: str,
    start_dt: datetime,
    end_dt: Optional[datetime],
):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            """INSERT INTO members
               (user_id, username, tariff, start_dt, end_dt, status, reminded_stage,
                in_chat, chat_status, last_synced_at)
               VALUES (:uid, :un, :tf, :sd, :ed, 'active', 0, FALSE, NULL, NULL)
               ON CONFLICT (user_id) DO UPDATE SET
               username=:un, tariff=:tf, start_dt=:sd, end_dt=:ed, status='active',
               reminded_stage=0""",
            uid=user_id,
            un=username,
            tf=tariff_key,
            sd=start_dt,
            ed=end_dt,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог записать участника %s: %s", user_id, e)
    finally:
        if conn:
            conn.close()


def update_member_presence(user_id: int, in_chat: bool, chat_status: str):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            """UPDATE members
               SET in_chat=:inch, chat_status=:status, last_synced_at=:ts
               WHERE user_id=:uid""",
            uid=user_id,
            inch=in_chat,
            status=chat_status,
            ts=datetime.now(),
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог обновить присутствие %s: %s", user_id, e)
    finally:
        if conn:
            conn.close()


def save_yoomoney_attempt(
    label: str,
    email: str,
    user_id: int,
    username: str,
    tariff_key: str,
    amount: int,
):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            """INSERT INTO yoomoney_attempts
               (label, email, user_id, username, tariff, amount, created_at,
                status, access_granted, user_notified, admin_notified)
               VALUES (:label, :email, :uid, :un, :tf, :amount, :ts,
                       'pending', FALSE, FALSE, FALSE)
               ON CONFLICT (label) DO UPDATE SET
               email=:email, user_id=:uid, username=:un, tariff=:tf,
               amount=:amount, created_at=:ts, status='pending',
               operation_id=NULL, paid_amount=NULL, paid_at=NULL,
               invite_link=NULL, access_start_dt=NULL, access_end_dt=NULL,
               access_granted=FALSE, user_notified=FALSE, admin_notified=FALSE""",
            label=label,
            email=email,
            uid=user_id,
            un=username or "—",
            tf=tariff_key,
            amount=amount,
            ts=datetime.now(),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог сохранить оплату ЮMoney %s: %s", label, e)
    finally:
        if conn:
            conn.close()


def load_yoomoney_attempt(label: str) -> Optional[dict]:
    if not DATABASE_URL:
        return None
    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT email, user_id, username, tariff, amount, status,
                      operation_id, paid_amount, paid_at, invite_link,
                      access_start_dt, access_end_dt, access_granted,
                      user_notified, admin_notified
               FROM yoomoney_attempts WHERE label=:label""",
            label=label,
        )
        if not rows:
            return None
        (
            email,
            user_id,
            username,
            tariff,
            amount,
            status,
            operation_id,
            paid_amount,
            paid_at,
            invite_link,
            access_start_dt,
            access_end_dt,
            access_granted,
            user_notified,
            admin_notified,
        ) = rows[0]
        return {
            "email": email,
            "user_id": user_id,
            "username": username,
            "tariff": tariff,
            "amount": amount,
            "status": status,
            "operation_id": operation_id,
            "paid_amount": paid_amount,
            "paid_at": paid_at,
            "invite_link": invite_link,
            "access_start_dt": access_start_dt,
            "access_end_dt": access_end_dt,
            "access_granted": bool(access_granted),
            "user_notified": bool(user_notified),
            "admin_notified": bool(admin_notified),
        }
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог прочитать оплату ЮMoney %s: %s", label, e)
        return None
    finally:
        if conn:
            conn.close()


def mark_yoomoney_payment_received(label: str, operation_id: str, paid_amount: float) -> bool:
    """pending -> paid. Повторное webhook не ломает обработанную запись."""
    if not DATABASE_URL:
        return False
    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """UPDATE yoomoney_attempts
               SET status='paid',
                   operation_id=COALESCE(operation_id, :operation_id),
                   paid_amount=COALESCE(paid_amount, :paid_amount),
                   paid_at=COALESCE(paid_at, :paid_at)
               WHERE label=:label AND status='pending'
               RETURNING label""",
            label=label,
            operation_id=operation_id,
            paid_amount=paid_amount,
            paid_at=datetime.now(),
        )
        return bool(rows)
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог отметить платёж %s: %s", label, e)
        return False
    finally:
        if conn:
            conn.close()


def save_attempt_access_period(label: str, start_dt: datetime, end_dt: Optional[datetime]):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            """UPDATE yoomoney_attempts
               SET access_start_dt=:start_dt, access_end_dt=:end_dt
               WHERE label=:label""",
            label=label,
            start_dt=start_dt,
            end_dt=end_dt,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог сохранить период доступа %s: %s", label, e)
    finally:
        if conn:
            conn.close()


def mark_access_granted(label: str, invite_link: str):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            """UPDATE yoomoney_attempts
               SET access_granted=TRUE, invite_link=:invite_link
               WHERE label=:label""",
            label=label,
            invite_link=invite_link,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог сохранить invite link %s: %s", label, e)
    finally:
        if conn:
            conn.close()


def mark_user_notified(label: str):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            "UPDATE yoomoney_attempts SET user_notified=TRUE WHERE label=:label",
            label=label,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отметить user_notified %s: %s", label, e)
    finally:
        if conn:
            conn.close()


def mark_admin_notified(label: str):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            "UPDATE yoomoney_attempts SET admin_notified=TRUE WHERE label=:label",
            label=label,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отметить admin_notified %s: %s", label, e)
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Telegram presence / sync
# ---------------------------------------------------------------------------


async def get_chat_presence(bot: Bot, user_id: int):
    """Возвращает (True/False/None, Telegram status)."""
    try:
        member = await bot.get_chat_member(CLUB_CHAT_ID, user_id)
        status = str(getattr(member, "status", "") or "")
        if status == "restricted":
            return bool(getattr(member, "is_member", False)), status
        if status in {"creator", "administrator", "member"}:
            return True, status
        if status in {"left", "kicked"}:
            return False, status
        return False, status or "unknown"
    except Exception as e:  # noqa: BLE001
        err = str(e).lower()
        if any(marker in err for marker in (
            "participant_id_invalid",
            "user_not_participant",
            "member not found",
            "user not found",
            "user_id_invalid",
        )):
            return False, "left"
        return None, f"error:{type(e).__name__}"


async def sync_members(bot: Bot) -> dict:
    """Проверяет всех активных покупателей, известных базе."""
    result = {
        "db_active": 0,
        "in_chat": 0,
        "absent": [],
        "errors": [],
        "group_total": None,
    }

    if not DATABASE_URL:
        result["errors"].append("DATABASE_URL не задан")
        return result

    try:
        result["group_total"] = await bot.get_chat_member_count(CLUB_CHAT_ID)
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"Не удалось получить число участников: {e}")

    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT user_id, username, tariff, end_dt
               FROM members
               WHERE status='active'
               ORDER BY end_dt NULLS LAST"""
        )
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"Не удалось прочитать базу: {e}")
        return result
    finally:
        if conn:
            conn.close()

    result["db_active"] = len(rows)

    for user_id, username, tariff, end_dt in rows:
        present, tg_status = await get_chat_presence(bot, int(user_id))
        if present is True:
            result["in_chat"] += 1
            update_member_presence(int(user_id), True, tg_status)
        elif present is False:
            update_member_presence(int(user_id), False, tg_status)
            result["absent"].append({
                "user_id": user_id,
                "username": username,
                "tariff": tariff,
                "end_dt": end_dt,
                "chat_status": tg_status,
            })
        else:
            result["errors"].append(f"@{username or '—'} ({user_id}): {tg_status}")

    return result


async def sync_report_text(bot: Bot) -> str:
    data = await sync_members(bot)
    group_total = data["group_total"] if data["group_total"] is not None else "не удалось получить"

    lines = [
        "🔄 <b>Синхронизация клуба</b>",
        "",
        f"👥 Всего участников в Telegram: <b>{group_total}</b>",
        f"💳 Активных оплаченных в базе: <b>{data['db_active']}</b>",
        f"✅ Из них сейчас в группе: <b>{data['in_chat']}</b>",
        f"⚠️ Оплаченных, но сейчас не в группе: <b>{len(data['absent'])}</b>",
    ]

    if data["absent"]:
        lines.append("")
        lines.append("<b>Нет в группе:</b>")
        for item in data["absent"][:50]:
            end_dt = item["end_dt"]
            until = f"до {end_dt.strftime('%d.%m.%Y')}" if end_dt else "навсегда"
            lines.append(
                f"• @{item['username'] or '—'} "
                f"(<code>{item['user_id']}</code>) — {until}"
            )
        if len(data["absent"]) > 50:
            lines.append(f"… ещё {len(data['absent']) - 50}")

    if data["errors"]:
        lines.append("")
        lines.append(f"❗ Ошибок проверки: <b>{len(data['errors'])}</b>")

    return "\n".join(lines)


async def background_sync(bot: Bot):
    """Периодически обновляет in_chat, но не спамит админа сообщениями."""
    try:
        data = await sync_members(bot)
        log.info(
            "Фоновая синхронизация: DB=%s, в группе=%s, отсутствуют=%s, ошибок=%s",
            data["db_active"],
            data["in_chat"],
            len(data["absent"]),
            len(data["errors"]),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка фоновой синхронизации: %s", e)


@router.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    """Поддерживает актуальность in_chat при новых входах/выходах."""
    if event.chat.id != CLUB_CHAT_ID:
        return

    tg_member = event.new_chat_member
    status = str(getattr(tg_member, "status", "") or "")
    user_id = int(tg_member.user.id)

    if status == "restricted":
        in_chat = bool(getattr(tg_member, "is_member", False))
    else:
        in_chat = status in {"creator", "administrator", "member"}

    if get_member_record(user_id):
        update_member_presence(user_id, in_chat, status)
        log.info("Членство %s: %s (%s)", user_id, in_chat, status)


# ---------------------------------------------------------------------------
# Сроки и напоминания
# ---------------------------------------------------------------------------


async def check_expired(bot: Bot):
    if not DATABASE_URL:
        return

    now = datetime.now()
    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT user_id, end_dt, username
               FROM members
               WHERE end_dt IS NOT NULL AND status='active'"""
        )
    except Exception as e:  # noqa: BLE001
        log.error("check_expired: ошибка базы: %s", e)
        return
    finally:
        if conn:
            conn.close()

    for user_id, end_dt, username in rows:
        if not end_dt or (end_dt - now).total_seconds() > 0:
            continue

        present, tg_status = await get_chat_presence(bot, int(user_id))
        if present is None:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ <b>Не удалось проверить истёкший доступ</b>\n"
                    f"@{username or '—'} (id <code>{user_id}</code>)\n"
                    f"Telegram: {tg_status}",
                )
            except Exception:
                pass
            continue

        if present:
            try:
                await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=int(user_id))
                await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=int(user_id))
            except Exception as e:  # noqa: BLE001
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ <b>Не удалось удалить истёкшего участника</b>\n\n"
                        f"@{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Причина: {e}",
                    )
                except Exception:
                    pass
                continue

        conn = None
        try:
            conn = get_db()
            conn.run(
                """UPDATE members
                   SET status='expired', in_chat=FALSE, chat_status=:chat_status
                   WHERE user_id=:uid""",
                uid=int(user_id),
                chat_status=tg_status or "left",
            )
        except Exception:
            pass
        finally:
            if conn:
                conn.close()

        try:
            await bot.send_message(
                int(user_id),
                "Твоя подписка на месяц закончилась, доступ в клуб закрыт 🤍\n\n"
                "Будем рады видеть снова — нажми /start, чтобы продлить.",
            )
        except Exception:
            pass

        if present:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🚪 <b>Участник удалён по окончании подписки</b>\n\n"
                    f"@{username or '—'} (id <code>{user_id}</code>)\n"
                    f"Срок закончился: {end_dt.strftime('%d.%m.%Y')}",
                )
            except Exception:
                pass


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
            [InlineKeyboardButton(text="💛 Продлить доступ", callback_data="renew_month")]
        ]
    )


async def check_reminders(bot: Bot):
    if not DATABASE_URL:
        return

    now = datetime.now()
    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT user_id, end_dt, reminded_stage
               FROM members
               WHERE end_dt IS NOT NULL AND status='active' AND tariff='month'"""
        )
    except Exception as e:  # noqa: BLE001
        log.error("check_reminders: ошибка базы: %s", e)
        return
    finally:
        if conn:
            conn.close()

    for user_id, end_dt, reminded in rows:
        days_left = (end_dt - now).total_seconds() / 86400
        reminded = reminded or 0

        if 0 < days_left <= 1 and reminded != 1:
            try:
                await bot.send_message(int(user_id), REMIND_1_DAY, reply_markup=renew_keyboard())
                conn = get_db()
                conn.run("UPDATE members SET reminded_stage=1 WHERE user_id=:uid", uid=int(user_id))
                conn.close()
            except Exception:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
        elif 1 < days_left <= 3 and reminded == 0:
            try:
                await bot.send_message(int(user_id), REMIND_3_DAYS, reply_markup=renew_keyboard())
                conn = get_db()
                conn.run("UPDATE members SET reminded_stage=3 WHERE user_id=:uid", uid=int(user_id))
                conn.close()
            except Exception:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass


async def build_status_text(bot: Optional[Bot] = None) -> str:
    if not DATABASE_URL:
        return "База не подключена, статус недоступен."

    now = datetime.now()
    conn = None
    try:
        conn = get_db()
        active_month = conn.run(
            """SELECT username, end_dt, in_chat
               FROM members
               WHERE status='active' AND end_dt IS NOT NULL
               ORDER BY end_dt"""
        )
        forever_rows = conn.run(
            """SELECT in_chat FROM members
               WHERE status='active' AND end_dt IS NULL"""
        )
        expired = conn.run("SELECT COUNT(*) FROM members WHERE status='expired'")
    except Exception as e:  # noqa: BLE001
        return f"Не смог прочитать базу: {e}"
    finally:
        if conn:
            conn.close()

    forever_n = len(forever_rows)
    expired_n = expired[0][0] if expired else 0
    active_total = len(active_month) + forever_n
    in_group = sum(1 for _u, _e, in_chat in active_month if in_chat) + sum(
        1 for (in_chat,) in forever_rows if in_chat
    )
    absent_n = max(0, active_total - in_group)

    group_total = None
    if bot:
        try:
            group_total = await bot.get_chat_member_count(CLUB_CHAT_ID)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог получить число участников группы: %s", e)

    lines = [
        "✅ <b>Бот на посту</b>",
        "",
        f"👥 Участников Telegram: <b>{group_total if group_total is not None else '—'}</b>",
        f"💳 Активных оплат в базе: <b>{active_total}</b>",
        f"✅ Активных оплаченных в группе по последнему синку: <b>{in_group}</b>",
        f"⚠️ Активных оплат не в группе/не проверенных: <b>{absent_n}</b>",
        f"💎 Навсегда: <b>{forever_n}</b>",
        f"🚪 Истёкших: <b>{expired_n}</b>",
    ]

    # Не показываем в ежедневном отчёте старые записи людей, которых давно нет.
    upcoming = [(u, e) for u, e, in_chat in active_month if in_chat]
    if upcoming:
        lines.append("")
        lines.append("<b>Ближайшие окончания среди тех, кто сейчас в группе:</b>")
        for username, end_dt in upcoming[:10]:
            days_left = max(0, int((end_dt - now).total_seconds() / 86400))
            mark = "⚠️" if days_left <= 3 else "🔹"
            lines.append(
                f"{mark} @{username or '—'} — до {end_dt.strftime('%d.%m.%Y')} "
                f"(осталось {days_left} дн.)"
            )

    return "\n".join(lines)


async def daily_report(bot: Bot):
    try:
        await sync_members(bot)
        today = datetime.now().strftime("%d.%m.%Y")
        text = await build_status_text(bot)
        await bot.send_message(
            ADMIN_ID,
            f"🌙 <b>Итог дня — {today}</b>\n\n{text}\n\n"
            "Для детальной сверки нажми «🔄 Синхронизация» в /admin.",
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отправить отчёт: %s", e)


async def retry_unfinished_deliveries(bot: Bot):
    """Повторяет выдачу/уведомления после временного сбоя Telegram."""
    if not DATABASE_URL:
        return

    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT label, email, user_id, username, tariff, operation_id, paid_amount
               FROM yoomoney_attempts
               WHERE status='paid'
                 AND (access_granted=FALSE OR user_notified=FALSE OR admin_notified=FALSE)
               ORDER BY paid_at NULLS LAST
               LIMIT 20"""
        )
    except Exception as e:  # noqa: BLE001
        log.error("retry_unfinished_deliveries: ошибка базы: %s", e)
        return
    finally:
        if conn:
            conn.close()

    for label, email, user_id, username, tariff_key, operation_id, paid_amount in rows:
        if not user_id or tariff_key not in TARIFFS:
            continue
        try:
            attempt = load_yoomoney_attempt(label)
            if not attempt:
                continue
            await grant_access(
                bot,
                label=label,
                email=email or "",
                amount=paid_amount or attempt.get("amount") or 0,
                currency="RUB",
                operation_id=operation_id or label,
                tariff_name=TARIFFS[tariff_key]["name"],
                tariff_key=tariff_key,
                user_id=int(user_id),
                username=username or "—",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Повторная выдача %s завершилась ошибкой: %s", label, e)


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------


def tariff_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💛 1 месяц — {TARIFFS['month']['price']}₽",
                callback_data="tariff_month",
            )],
            [InlineKeyboardButton(
                text=f"💎 Навсегда — {TARIFFS['forever']['price']}₽",
                callback_data="tariff_forever",
            )],
            [InlineKeyboardButton(
                text="⭐ Отзывы участников",
                url="https://t.me/creator_lab_otzyvy",
            )],
        ]
    )


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
            [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_oferta")]
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
            [InlineKeyboardButton(text="✅ Я соглашаюсь", callback_data="accept_warning")]
        ]
    )


# ---------------------------------------------------------------------------
# Админ-панель
# ---------------------------------------------------------------------------


def _is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


def _clean_username(raw: str) -> str:
    return raw.strip().lstrip("@").strip()


def _parse_target(raw: str):
    target = raw.strip().lstrip("@").strip()
    if target.lstrip("-").isdigit():
        return int(target), None
    return None, target


async def _lookup_in_db(user_id=None, username=None):
    if not DATABASE_URL:
        return None, None
    conn = None
    try:
        conn = get_db()
        if user_id is not None:
            rows = conn.run(
                "SELECT user_id, username FROM members WHERE user_id=:uid",
                uid=user_id,
            )
        else:
            rows = conn.run(
                "SELECT user_id, username FROM members WHERE LOWER(username)=LOWER(:un)",
                un=_clean_username(username or ""),
            )
        if rows:
            return rows[0][0], rows[0][1]
    except Exception as e:  # noqa: BLE001
        log.error("lookup: %s", e)
    finally:
        if conn:
            conn.close()
    return None, None


def _is_blacklisted(user_id: int, username: Optional[str]) -> bool:
    if not DATABASE_URL:
        return False
    conn = None
    try:
        conn = get_db()
        uname = _clean_username(username or "") if username else None
        if user_id and uname:
            rows = conn.run(
                """SELECT 1 FROM blacklisted_users
                   WHERE user_id=:uid OR LOWER(username)=LOWER(:un) LIMIT 1""",
                uid=user_id,
                un=uname,
            )
        elif user_id:
            rows = conn.run(
                "SELECT 1 FROM blacklisted_users WHERE user_id=:uid LIMIT 1",
                uid=user_id,
            )
        elif uname:
            rows = conn.run(
                "SELECT 1 FROM blacklisted_users WHERE LOWER(username)=LOWER(:un) LIMIT 1",
                un=uname,
            )
        else:
            rows = []
        return bool(rows)
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


def _add_to_blacklist(user_id: Optional[int], username: Optional[str]):
    if not DATABASE_URL:
        return
    conn = None
    try:
        conn = get_db()
        uname = _clean_username(username or "") if username else None
        if user_id:
            # Поле user_id UNIQUE, поэтому для числового id работает upsert.
            conn.run(
                """INSERT INTO blacklisted_users (user_id, username, added_at)
                   VALUES (:uid, :un, :ts)
                   ON CONFLICT (user_id) DO UPDATE SET username=:un""",
                uid=user_id,
                un=uname,
                ts=datetime.now(),
            )
        elif uname:
            # Без user_id PostgreSQL не даст уникально обновить строку,
            # поэтому такие записи лучше не создавать через /ban.
            log.warning("Чёрный список без user_id не добавлен: @%s", uname)
    finally:
        if conn:
            conn.close()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💛 Цена месяца: {TARIFFS['month']['price']}₽",
                callback_data="admin_setprice:month",
            )],
            [InlineKeyboardButton(
                text=f"💎 Цена навсегда: {TARIFFS['forever']['price']}₽",
                callback_data="admin_setprice:forever",
            )],
            [InlineKeyboardButton(
                text="🔄 Синхронизировать группу",
                callback_data="admin_sync",
            )],
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="admin_status"),
                InlineKeyboardButton(text="👥 Участники", callback_data="admin_members"),
            ],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "⚙️ <b>Панель управления</b>\n\n"
        f"💛 Тариф «1 месяц»: <b>{TARIFFS['month']['price']}₽</b>\n"
        f"💎 Тариф «Навсегда»: <b>{TARIFFS['forever']['price']}₽</b>\n\n"
        "Выбирай действие:",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data.startswith("admin_setprice:"))
async def on_admin_setprice_btn(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    tariff_key = cb.data.split(":", 1)[1]
    if tariff_key not in TARIFFS:
        await cb.answer("Неизвестный тариф", show_alert=True)
        return
    await state.update_data(tariff=tariff_key)
    await state.set_state(AdminPrice.waiting_for_price)
    await cb.message.answer(
        f"Напиши новую цену в рублях для тарифа <b>«{TARIFFS[tariff_key]['name']}»</b> (только цифры):"
    )
    await cb.answer()


@router.callback_query(F.data == "admin_sync")
async def on_admin_sync(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    await cb.answer("Проверяю группу…")
    try:
        await cb.message.answer(await sync_report_text(bot), reply_markup=admin_keyboard())
    except Exception as e:  # noqa: BLE001
        await cb.message.answer(f"❌ Ошибка синхронизации: {e}", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin_status")
async def on_admin_status(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    await cb.answer()
    await sync_members(bot)
    await cb.message.answer(await build_status_text(bot), reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin_members")
async def on_admin_members(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    await cb.answer()
    await sync_members(bot)
    await cb.message.answer(await members_report_text(), reply_markup=admin_keyboard())


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
        _set_db_setting(f"price_{tariff_key}", str(new_price))
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
    if len(parts) < 3 or parts[1] not in ("month", "forever") or not parts[2].isdigit() or int(parts[2]) <= 0:
        await message.answer(
            "Пример: <code>/setprice month 699</code> или "
            "<code>/setprice forever 9990</code>"
        )
        return
    tariff_key, price = parts[1], int(parts[2])
    TARIFFS[tariff_key]["price"] = price
    _set_db_setting(f"price_{tariff_key}", str(price))
    await message.answer(
        f"✅ Цена тарифа «{TARIFFS[tariff_key]['name']}» теперь <b>{price}₽</b>."
    )


@router.message(Command("status"))
async def cmd_status(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    await sync_members(bot)
    await message.answer(await build_status_text(bot))


async def members_report_text() -> str:
    if not DATABASE_URL:
        return "База не подключена."

    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT user_id, username, tariff, start_dt, end_dt,
                      status, in_chat, chat_status
               FROM members
               WHERE status='active'
               ORDER BY end_dt NULLS LAST, username"""
        )
    except Exception as e:  # noqa: BLE001
        return f"Ошибка базы: {e}"
    finally:
        if conn:
            conn.close()

    if not rows:
        return "В базе пока нет активных оплаченных участников."

    lines = [f"👥 <b>Активных оплат: {len(rows)}</b>", ""]
    for i, (uid, username, tariff, _start, end_dt, _status, in_chat, _chat_status) in enumerate(rows, 1):
        if end_dt:
            days = max(0, int((end_dt - datetime.now()).total_seconds() / 86400))
            period = f"до {end_dt.strftime('%d.%m.%Y')} ({days} дн.)"
        else:
            period = "навсегда"
        presence = "✅ в группе" if in_chat else "⚠️ не в группе"
        lines.append(
            f"{i}. @{username or '—'} <code>{uid}</code> — {period} — {presence}"
        )
        if i >= 80:
            lines.append(f"\n… показаны первые 80 из {len(rows)}")
            break

    return "\n".join(lines)


@router.message(Command("members"))
async def cmd_members(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    await sync_members(bot)
    await message.answer(await members_report_text())


@router.message(Command("find"))
async def cmd_find(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пример: /find @username или /find 123456789")
        return

    uid, uname = _parse_target(parts[1])
    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer("Пользователь не найден в базе.")
        return

    record = get_member_record(uid)
    present, tg_status = await get_chat_presence(bot, uid)
    if not record:
        await message.answer(
            f"⚠️ <b>В базе записи нет</b>\n"
            f"id: <code>{uid}</code>\n"
            f"В Telegram: {'✅ в группе' if present else '❌ не в группе'} ({tg_status})"
        )
        return

    end_dt = record["end_dt"]
    period = end_dt.strftime("%d.%m.%Y") if end_dt else "навсегда"
    await message.answer(
        "🔎 <b>Пользователь найден</b>\n\n"
        f"username: @{record['username'] or '—'}\n"
        f"id: <code>{uid}</code>\n"
        f"тариф: {record['tariff']}\n"
        f"статус оплаты: {record['status']}\n"
        f"доступ до: {period}\n"
        f"в группе сейчас: {'✅ да' if present else '❌ нет'}\n"
        f"Telegram status: {tg_status}"
    )


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
        await message.answer(f"Не знаю id для @{uname}. Укажи числовой id.")
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
        if days <= 0:
            await message.answer("Количество дней должно быть больше нуля.")
            return
        end_dt = start_dt + timedelta(days=days)
        tariff = "month"

    save_member(uid, uname or "—", tariff, start_dt, end_dt)
    await message.answer(f"✅ Добавлен @{uname or uid} ({term}).")


@router.message(Command("extend"))
async def cmd_extend(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[2].isdigit() or int(parts[2]) <= 0:
        await message.answer("Пример: /extend @username 30")
        return

    uid, uname = _parse_target(parts[1])
    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer("Пользователь не найден.")
        return

    record = get_member_record(uid)
    if not record:
        await message.answer("Пользователя нет в базе оплаченных участников.")
        return
    if record["end_dt"] is None:
        await message.answer("У пользователя доступ навсегда 💎. Продлевать нечего.")
        return

    new_end = max(datetime.now(), record["end_dt"]) + timedelta(days=int(parts[2]))
    conn = None
    try:
        conn = get_db()
        conn.run(
            """UPDATE members
               SET end_dt=:end_dt, status='active', reminded_stage=0
               WHERE user_id=:uid""",
            uid=uid,
            end_dt=new_end,
        )
        await message.answer(
            f"✅ @{uname or uid} продлён ещё на {parts[2]} дней.\n"
            f"Новый срок: <b>{new_end.strftime('%d.%m.%Y')}</b>"
        )
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка базы: {e}")
    finally:
        if conn:
            conn.close()


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

    conn = None
    try:
        conn = get_db()
        conn.run(
            """UPDATE members
               SET status='expired', in_chat=FALSE, chat_status='removed_by_admin'
               WHERE user_id=:uid""",
            uid=uid,
        )
    except Exception:
        pass
    finally:
        if conn:
            conn.close()

    try:
        await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await message.answer(f"✅ @{uname or uid} удалён из клуба.")
    except Exception as e:  # noqa: BLE001
        await message.answer(
            f"✅ В базе доступ закрыт.\n⚠️ В Telegram удалить участника не удалось: {e}"
        )


@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пример: /ban @username или /ban 123456789")
        return

    uid, uname = _parse_target(parts[1])
    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer("Пользователь не найден в базе. Для блокировки нужен числовой id.")
        return

    _add_to_blacklist(uid, uname)

    conn = None
    try:
        conn = get_db()
        conn.run(
            """UPDATE members
               SET status='expired', in_chat=FALSE, chat_status='blacklisted'
               WHERE user_id=:uid""",
            uid=uid,
        )
    except Exception:
        pass
    finally:
        if conn:
            conn.close()

    try:
        await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await message.answer(f"⛔ @{uname or uid} добавлен в чёрный список и заблокирован.")
    except Exception as e:  # noqa: BLE001
        await message.answer(
            f"⛔ @{uname or uid} добавлен в чёрный список.\n"
            f"⚠️ Заблокировать в Telegram не удалось: {e}"
        )


@router.message(Command("sync"))
async def cmd_sync(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    await message.answer("🔄 Запускаю сверку базы с Telegram-группой…")
    await message.answer(await sync_report_text(bot))


@router.message(Command("resend"))
async def cmd_resend(message: Message, bot: Bot):
    """Ручная повторная отправка последней ссылки пользователя."""
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пример: /resend @username или /resend 123456789")
        return
    uid, uname = _parse_target(parts[1])
    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer("Пользователь не найден.")
        return

    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT label, invite_link, email, tariff, status, access_granted
               FROM yoomoney_attempts
               WHERE user_id=:uid AND access_granted=TRUE
               ORDER BY created_at DESC
               LIMIT 1""",
            uid=uid,
        )
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка базы: {e}")
        return
    finally:
        if conn:
            conn.close()

    if not rows:
        await message.answer("Для этого пользователя нет оплаченной выдачи доступа.")
        return

    label, _old_link, email, tariff, status, _access_granted = rows[0]
    try:
        link = await bot.create_chat_invite_link(
            chat_id=CLUB_CHAT_ID,
            member_limit=1,
            name=f"resend {uid}",
        )
        invite_link = link.invite_link
        mark_access_granted(label, invite_link)

        await bot.send_message(
            uid,
            f"Вот новая персональная ссылка в Creator Lab:\n{invite_link}\n\n"
            "Эта ссылка одноразовая. Если возникнет ошибка, напиши администратору."
        )
        await message.answer(f"✅ Новая ссылка отправлена @{uname or uid}.\nМетка: <code>{label}</code>")
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ Не удалось создать/отправить новую ссылку: {e}")


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "/admin — панель управления\n"
        "/setprice month 699 — изменить цену месяца\n"
        "/setprice forever 9990 — изменить цену навсегда\n"
        "/status — актуальный статус и число участников группы\n"
        "/sync — сверить оплаченных с Telegram\n"
        "/members — активные оплаты\n"
        "/find @username — найти человека\n"
        "/add @username 30 — добавить на 30 дней\n"
        "/add @username forever — добавить навсегда\n"
        "/extend @username 30 — продлить на 30 дней\n"
        "/remove @username — кикнуть из клуба\n"
        "/ban @username — чёрный список + бан\n"
        "/resend @username — повторно отправить сохранённую ссылку\n"
    )


# ---------------------------------------------------------------------------
# Пользовательский путь
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
    except Exception:
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
        f"Тариф «{TARIFFS[tariff_key]['name']}» — отличный выбор 🙂\n\n{ASK_EMAIL}"
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
    if tariff_key not in TARIFFS:
        await message.answer("Сессия сброшена. Нажми /start заново.")
        await state.clear()
        return

    if not YOOMONEY_RECEIVER or not YOOMONEY_NOTIFICATION_SECRET:
        await message.answer("Оплата настраивается. Напиши @adelin_creator.")
        await state.clear()
        return

    price = int(TARIFFS[tariff_key]["price"])
    label = f"ym_{user.id}_{uuid.uuid4().hex[:12]}"
    save_yoomoney_attempt(label, email, user.id, user.username, tariff_key, price)
    await state.clear()

    url = f"{PUBLIC_BASE_URL}/yoomoney/pay/{label}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Перейти к оплате 💳", url=url)
        ]]
    )
    await message.answer(
        f"Готово! Нажми кнопку ниже и оплати доступ ({price}₽) через ЮMoney.\n\n"
        "Как только оплата пройдёт, я пришлю ссылку в клуб прямо сюда.",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# Выдача доступа
# ---------------------------------------------------------------------------


async def grant_access(
    bot: Bot,
    label: str,
    email: str,
    amount,
    currency: str,
    operation_id: str,
    tariff_name: str,
    tariff_key: str,
    user_id: int,
    username: str,
) -> bool:
    username = username or "—"

    if _is_blacklisted(user_id, username):
        try:
            await bot.send_message(user_id, BLACKLIST_NOTICE)
        except Exception:
            pass
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⛔ <b>Платёж от пользователя из чёрного списка</b>\n"
                f"@{username} (id <code>{user_id}</code>)\n"
                f"Сумма: {amount} {currency}\n"
                f"Метка: <code>{label}</code>",
            )
        except Exception:
            pass
        return False

    attempt = load_yoomoney_attempt(label)
    if not attempt:
        log.error("grant_access: платеж %s отсутствует в базе", label)
        return False

    now = datetime.now()
    existing = get_member_record(user_id)

    # ВАЖНО: период конкретного платежа сохраняем в yoomoney_attempts.
    # Тогда повторный webhook или фоновая повторная доставка не продлит подписку второй раз.
    if attempt.get("access_start_dt") is not None:
        start_dt = attempt["access_start_dt"]
        end_dt = attempt.get("access_end_dt")
    elif tariff_key == "forever":
        start_dt = existing["start_dt"] if existing and existing.get("start_dt") else now
        end_dt = None
        save_attempt_access_period(label, start_dt, end_dt)
    else:
        if existing and existing.get("status") == "active" and existing.get("end_dt"):
            start_dt = existing.get("start_dt") or now
            base = max(now, existing["end_dt"])
        else:
            start_dt = now
            base = now
        end_dt = base + timedelta(days=30)
        save_attempt_access_period(label, start_dt, end_dt)

    # После оплаты ссылка должна быть персональной и одноразовой.
    invite_url = attempt.get("invite_link")
    if not invite_url:
        try:
            link = await bot.create_chat_invite_link(
                chat_id=CLUB_CHAT_ID,
                member_limit=1,
                name=f"buyer {user_id}",
            )
            invite_url = link.invite_link
            mark_access_granted(label, invite_url)
        except Exception as e:  # noqa: BLE001
            log.exception("Не смог создать ссылку %s: %s", label, e)
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"❗ <b>Оплата есть, но ссылку создать не удалось</b>\n\n"
                    f"Покупатель: @{username} (id <code>{user_id}</code>)\n"
                    f"Почта: {email or '—'}\n"
                    f"Сумма: {amount} {currency}\n"
                    f"Причина: {e}\n"
                    f"Метка: <code>{label}</code>",
                )
            except Exception:
                pass
            return False

    save_member(user_id, username, tariff_key, start_dt, end_dt)

    # Пользовательское уведомление можно безопасно повторить при сбое.
    delivery_ok = bool(attempt.get("user_notified"))
    if not delivery_ok:
        try:
            if end_dt:
                period_line = (
                    f"📅 Доступ активен до <b>{end_dt.strftime('%d.%m.%Y')}</b>.\n"
                    "Я напомню тебе об окончании заранее 🤍\n\n"
                )
            else:
                period_line = "📅 Доступ — <b>навсегда</b> 💎\n\n"

            await bot.send_message(
                user_id,
                "Оплата получена, спасибо! 💛\n\n"
                "Добро пожаловать в Creator Lab 🔐\n\n"
                f"{period_line}"
                f"Вот твоя персональная ссылка в клуб:\n{invite_url}\n\n"
                "Заходи и пользуйся 🚀",
            )
            mark_user_notified(label)
            delivery_ok = True
        except Exception as e:  # noqa: BLE001
            log.error("Не смог доставить ссылку клиенту %s: %s", user_id, e)
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ <b>Оплата получена, но пользователь не получил сообщение</b>\n"
                    f"@{username} (id <code>{user_id}</code>)\n"
                    f"Причина: {e}\n"
                    f"Метка: <code>{label}</code>\n"
                    f"Ссылка для ручной отправки: {invite_url}",
                )
            except Exception:
                pass

    # Админское уведомление независимо от доставки клиенту.
    admin_ok = bool(attempt.get("admin_notified"))
    if not admin_ok:
        try:
            access_str = f"до {end_dt.strftime('%d.%m.%Y')}" if end_dt else "навсегда"
            await bot.send_message(
                ADMIN_ID,
                f"💰 <b>Новая продажа!</b>\n\n"
                f"Тариф: {tariff_name}\n"
                f"Покупатель: @{username} (id <code>{user_id}</code>)\n"
                f"Почта: {email or '—'}\n"
                f"Сумма: {amount} {currency}\n"
                f"Метка: <code>{label}</code>\n"
                f"Операция: <code>{operation_id}</code>\n"
                f"Срок: {access_str}\n"
                f"Пользователь получил ссылку: {'✅' if delivery_ok else '❌'}\n"
                f"Ссылка: {invite_url}",
            )
            mark_admin_notified(label)
            admin_ok = True
        except Exception as e:  # noqa: BLE001
            log.error("Не смог отправить уведомление админу %s: %s", ADMIN_ID, e)

    # Фиксируем, что ссылка существует. Не зависит от доставки сообщений.
    mark_access_granted(label, invite_url)
    return delivery_ok or admin_ok


# ---------------------------------------------------------------------------
# ЮMoney webhook
# ---------------------------------------------------------------------------


def verify_yoomoney_sign(params: dict[str, str]) -> bool:
    """
    Новый алгоритм ЮMoney:
    - удалить только sign;
    - отсортировать остальные параметры по имени;
    - URL-encode значения в RFC 3986;
    - соединить key=value через &;
    - HMAC-SHA256 с секретом HTTP-уведомлений.

    Старый SHA-1 оставлен только для старых уведомлений, в которых sign отсутствует.
    """
    if not YOOMONEY_NOTIFICATION_SECRET:
        log.error("YOOMONEY_NOTIFICATION_SECRET не задан.")
        return False

    secret = YOOMONEY_NOTIFICATION_SECRET
    sign = (params.get("sign") or "").strip().lower()

    if sign:
        signing_parts = []
        for key in sorted(k for k in params.keys() if k != "sign"):
            value = "" if params.get(key) is None else str(params.get(key))
            signing_parts.append(f"{key}={quote(value, safe='-_.~')}")
        signing_string = "&".join(signing_parts)

        calculated = hmac.new(
            secret.encode("utf-8"),
            signing_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().lower()

        ok = hmac.compare_digest(calculated, sign)
        if not ok:
            log.warning(
                "ЮMoney: неверный sign для operation_id=%s. received=%s..., calculated=%s...",
                params.get("operation_id"),
                sign[:12],
                calculated[:12],
            )
        return ok

    # Legacy формат до перехода на sign.
    old_hash = (params.get("sha1_hash") or "").strip().lower()
    if not old_hash:
        return False

    fields = [
        params.get("notification_type", ""),
        params.get("operation_id", ""),
        params.get("amount", ""),
        params.get("currency", ""),
        params.get("datetime", ""),
        params.get("sender", ""),
        params.get("codepro", ""),
        secret,
        params.get("label", ""),
    ]
    raw = "&".join(str(item) for item in fields)
    calculated = hashlib.sha1(raw.encode("utf-8")).hexdigest().lower()
    return hmac.compare_digest(calculated, old_hash)


async def handle_yoomoney_pay(request: web.Request) -> web.Response:
    label = request.match_info.get("label", "")
    info = load_yoomoney_attempt(label)
    if not info:
        return web.Response(status=404, text="payment not found")
    if info.get("status") == "paid" and info.get("access_granted"):
        return web.Response(status=404, text="payment already processed")
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
<head><meta charset="utf-8"><title>Оплата Creator Lab</title></head>
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
        params = {key: str(value) for key, value in form.items()}
    except Exception as e:  # noqa: BLE001
        log.error("ЮMoney: ошибка чтения формы: %s", e)
        return web.Response(status=400, text="BAD REQUEST")

    if not params:
        return web.Response(status=400, text="EMPTY")

    # sign не логируем. Остальные параметры полезны для диагностики.
    safe_log = dict(params)
    safe_log.pop("sign", None)
    log.info(
        "🔔 [ЮMoney Webhook] Данные формы: %s",
        json.dumps(safe_log, ensure_ascii=False)[:2500],
    )

    if not verify_yoomoney_sign(params):
        # ВАЖНО: 200 означал бы для ЮMoney, что уведомление принято.
        # Поэтому при неверной подписи отдаём 403 и позволяем повторить доставку.
        return web.Response(status=403, text="INVALID SIGN")

    if str(params.get("test_notification", "")).lower() == "true":
        log.info("ЮMoney: тестовое уведомление, доступ не выдаём.")
        return web.Response(status=200, text="OK")

    label = params.get("label") or ""
    if not label:
        log.warning("ЮMoney: уведомление без label.")
        return web.Response(status=200, text="OK")

    info = load_yoomoney_attempt(label)
    if not info:
        log.error("ЮMoney: label %s не найден в базе.", label)
        # Чужой/старый label не поможет повтором.
        return web.Response(status=200, text="OK")

    expected = int(info.get("amount") or 0)
    # withdraw_amount = сколько списали с отправителя.
    # amount = сколько зачислили на кошелёк после комиссии.
    paid_raw = params.get("withdraw_amount") or params.get("amount") or "0"
    try:
        paid = float(str(paid_raw).replace(",", "."))
    except ValueError:
        paid = 0.0

    if paid + 0.01 < expected:
        log.warning(
            "ЮMoney: сумма меньше ожидаемой %s < %s для label=%s",
            paid,
            expected,
            label,
        )
        try:
            await request.app["bot"].send_message(
                ADMIN_ID,
                f"⚠️ <b>ЮMoney: недостаточная сумма</b>\n"
                f"Метка: <code>{label}</code>\n"
                f"Ожидалось: {expected} ₽\n"
                f"Получено: {paid_raw} ₽",
            )
        except Exception:
            pass
        return web.Response(status=200, text="OK")

    operation_id = params.get("operation_id") or label

    if info.get("status") == "pending":
        marked = mark_yoomoney_payment_received(label, operation_id, paid)
        if not marked:
            # Возможно, webhook уже обрабатывается другим экземпляром или база временно недоступна.
            refreshed = load_yoomoney_attempt(label)
            if not refreshed or refreshed.get("status") != "paid":
                log.error("ЮMoney: не удалось безопасно отметить %s как paid.", label)
                return web.Response(status=500, text="DATABASE RETRY")

    info = load_yoomoney_attempt(label) or info
    if info.get("status") != "paid":
        log.error("ЮMoney: платеж %s всё ещё не имеет статуса paid.", label)
        return web.Response(status=500, text="PAYMENT NOT CLAIMED")

    user_id = info.get("user_id")
    if not user_id:
        log.error("ЮMoney: в платеже %s нет user_id", label)
        return web.Response(status=500, text="MISSING USER")

    tariff_key = info.get("tariff")
    tariff_name = TARIFFS.get(tariff_key, {}).get("name", "—")

    await grant_access(
        request.app["bot"],
        label=label,
        email=info.get("email") or "",
        amount=paid_raw,
        currency="RUB",
        operation_id=operation_id,
        tariff_name=tariff_name,
        tariff_key=tariff_key,
        user_id=int(user_id),
        username=info.get("username") or "—",
    )

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
                BotCommand(command="admin", description="⚙️ Панель админа"),
                BotCommand(command="status", description="📊 Статус"),
                BotCommand(command="sync", description="🔄 Сверить группу"),
                BotCommand(command="members", description="👥 Оплаченные"),
                BotCommand(command="find", description="🔎 Найти"),
                BotCommand(command="add", description="➕ Добавить"),
                BotCommand(command="extend", description="➕ Продлить"),
                BotCommand(command="remove", description="🚪 Удалить"),
                BotCommand(command="ban", description="⛔ Заблокировать"),
                BotCommand(command="resend", description="📩 Повторить ссылку"),
                BotCommand(command="help", description="🛠 Все команды"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )

        me = await bot.get_me()
        log.info("Бот запущен: @%s (id=%s)", me.username, me.id)

        try:
            count = await bot.get_chat_member_count(CLUB_CHAT_ID)
            log.info("Telegram-клуб %s: участников=%s", CLUB_CHAT_ID, count)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог получить число участников клуба: %s", e)

    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось настроить меню: %s", e)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в переменных окружения")

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    init_db()
    await on_startup(bot)

    # После запуска сразу сверяем известных покупателей.
    try:
        await background_sync(bot)
    except Exception as e:  # noqa: BLE001
        log.warning("Стартовый sync не выполнен: %s", e)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expired, "interval", hours=1, args=[bot])
    scheduler.add_job(check_reminders, "interval", hours=12, args=[bot])
    scheduler.add_job(background_sync, "interval", hours=6, args=[bot])
    scheduler.add_job(retry_unfinished_deliveries, "interval", minutes=10, args=[bot])
    scheduler.add_job(daily_report, CronTrigger(hour=20, minute=0), args=[bot])
    scheduler.start()

    app = web.Application()
    app["bot"] = bot

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/yoomoney/pay/{label}", handle_yoomoney_pay)

    # Оставляем совместимость со старым URL, в том числе:
    # /yoomoney/notification/adelin_secret_2026
    # Криптографическая защита всё равно идёт через sign.
    for path in (
        "/yoomoney/notification",
        "/yoomoney/notification/",
        "/yoomoney/notification/{secret}",
        "/yoomoney/notification/{secret}/",
    ):
        app.router.add_get(path, handle_check)
        app.router.add_post(path, handle_yoomoney_notification)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP-сервер слушает порт %s.", PORT)

    try:
        # Бот работает через polling, HTTP-сервер отдельно принимает ЮMoney.
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
