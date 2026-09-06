# -*- coding: utf-8 -*-
"""
 Бот доступа в клуб Creator Lab с оплатой через ЮMoney.

Два тарифа:
  - month   : подписка на 1 месяц
  - forever : разовая покупка навсегда

Логика:
  1. /start -> приветствие + кнопки выбора тарифа.
  2. Бот просит почту (на неё придёт чек) и сохраняет username.
  3. Бот создаёт ссылку на оплату ЮMoney.
  4. После оплаты ЮMoney стучится на /yoomoney/notification.
  5. Бот опознаёт покупателя по label,
      выдаёт одноразовую ссылку в клуб и шлёт уведомление админу.

Админ может менять цены прямо в Telegram:
  /admin
  /setprice month 699
  /setprice forever 9990

Все секреты берутся из переменных окружения (Render -> Environment).
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
from urllib.parse import quote

import pg8000.native
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from aiohttp import web, ClientSession

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
LAVA_API_KEY = os.getenv("LAVA_API_KEY", "")

CURRENCY = os.getenv("CURRENCY", "RUB")
PAYMENT_METHOD = os.getenv("PAYMENT_METHOD", "BANK131")  # метод для карты (v2)

CLUB_CHAT_ID = int(os.getenv("CLUB_CHAT_ID") or os.getenv("CHANNEL_ID") or "-1003973853516")  # id клуба (с -100)
ADMIN_ID = int(os.getenv("ADMIN_ID") or os.getenv("OWNER_ID") or "1619432734")                # кому слать уведомления

DATABASE_URL = os.getenv("DATABASE_URL", "")  # PostgreSQL на Render — для учёта сроков подписки

# секрет в адресе вебхука (/lava/<secret>)
WEBHOOK_SECRET = (
    os.getenv("WEBHOOK_SECRET")
    or os.getenv("YOOMONEY_WEBHOOK_SECRET")
    or os.getenv("LAVA_WEBHOOK_SECRET_PATH")
    or "change-me-please"
).strip().strip("/")

PORT = int(os.getenv("PORT", "10000"))
LAVA_BASE = "https://gate.lava.top"

# ЮMoney кошелек: форма оплаты + HTTP-уведомления.
YOOMONEY_RECEIVER = os.getenv("YOOMONEY_RECEIVER", "").strip()  # номер кошелька 4100...
YOOMONEY_NOTIFICATION_SECRET = os.getenv("YOOMONEY_NOTIFICATION_SECRET", "").strip()
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("APP_URL")
    or "https://adelin-miller.onrender.com"
).rstrip("/")

# ---------------------------------------------------------------------------
# ТАРИФЫ. Для каждого — свои product_id, offer_id и цена (всё из env).
# offer_id можно оставить пустым — бот попробует найти его сам по product_id.
# ---------------------------------------------------------------------------

TARIFFS = {
    "month": {
        "name": "1 месяц",
        "product_id": os.getenv("LAVA_PRODUCT_ID_MONTH", "").strip(),
        "offer_id": os.getenv("LAVA_OFFER_ID_MONTH", "").strip(),
        "price": int(os.getenv("PRICE_MONTH", "899")),
        "periodicity": "ONE_TIME",  # продукт пересоздан как разовый (ради СБП)
    },
    "forever": {
        "name": "Навсегда",
        "product_id": os.getenv("LAVA_PRODUCT_ID_FOREVER", "").strip(),
        "offer_id": os.getenv("LAVA_OFFER_ID_FOREVER", "").strip(),
        "price": int(os.getenv("PRICE_FOREVER", "9990")),
        "periodicity": "ONE_TIME",  # разовая покупка
    },
}

# product_id -> ключ тарифа, чтобы по вебхуку понять, что купили
PRODUCT_TO_TARIFF = {}

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

# ---------------------------------------------------------------------------
# Хранилище (в памяти)
# ---------------------------------------------------------------------------

pending: dict[str, dict] = {}          # email_lower -> {user_id, username, tariff}
processed_contracts: set[str] = set()  # чтобы не выдать доступ дважды
resolved_offers: dict[str, str] = {}   # tariff_key -> offer_id (кэш)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Buy(StatesGroup):
    waiting_email = State()
    waiting_method = State()


class AdminPrice(StatesGroup):
    waiting_for_price = State()


router = Router()


# ---------------------------------------------------------------------------
# lava.top: вспомогательные функции
# ---------------------------------------------------------------------------

def _find_offer_id(node: Any, product_id: str) -> Optional[str]:
    """Рекурсивно ищем offerId внутри ответа /api/v2/products."""
    if isinstance(node, dict):
        if node.get("id") == product_id:
            offers = node.get("offers")
            if isinstance(offers, list) and offers:
                first = offers[0]
                if isinstance(first, dict) and first.get("id"):
                    return first["id"]
            if node.get("offerId"):
                return node["offerId"]
        for value in node.values():
            found = _find_offer_id(value, product_id)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_offer_id(item, product_id)
            if found:
                return found
    return None


async def handle_yoomoney_notification(request: web.Request) -> web.Response:
    # Принимаем уведомление независимо от пути, логируем сразу
    try:
        form = await request.post()
        params = {k: str(v) for k, v in form.items()}
    except Exception as e:
        log.error("Ошибка чтения данных ЮMoney: %s", e)
        return web.Response(status=400, text="bad request")

    log.info("🔔 Получено уведомление ЮMoney: %s", json.dumps(params, ensure_ascii=False)[:2000])

    t = TARIFFS[tariff_key]

    if t["offer_id"]:
        resolved_offers[tariff_key] = t["offer_id"]
        log.info("[%s] offerId из env: %s", tariff_key, t["offer_id"])
        return t["offer_id"]

    try:
        async with session.get(
            f"{LAVA_BASE}/api/v2/products",
            headers={"X-Api-Key": LAVA_API_KEY, "accept": "application/json"},
            timeout=30,
        ) as resp:
            text = await resp.text()
            if resp.status == 200:
                data = json.loads(text)
                found = _find_offer_id(data, t["product_id"])
                if found:
                    resolved_offers[tariff_key] = found
                    log.info("[%s] offerId найден автоматически: %s", tariff_key, found)
                    return found
                log.warning("[%s] offerId не найден в продуктах. Ответ: %s", tariff_key, text[:1200])
            else:
                log.warning("[%s] GET /products -> %s: %s", tariff_key, resp.status, text[:1200])
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] Не удалось получить продукты: %s", tariff_key, e)

    resolved_offers[tariff_key] = t["product_id"]
    log.info("[%s] используем product_id как offerId: %s", tariff_key, t["product_id"])
    return t["product_id"]


async def create_invoice(
    session: ClientSession, email: str, user_id: int, username: str,
    tariff_key: str, method: str = "card", price_override: Optional[int] = None,
) -> Optional[str]:
    """Создаёт счёт в lava.top и возвращает ссылку на оплату."""
    t = TARIFFS[tariff_key]
    invoice_price = price_override or t["price"]
    offer_id = await resolve_offer_id(session, tariff_key)

    client_utm = {
        "utm_source": "tgbot",
        "utm_content": str(user_id),
        "utm_term": username or "",
        "utm_campaign": tariff_key,
    }

    if method == "sbp":
        endpoint = f"{LAVA_BASE}/api/v3/invoice"
        body = {
            "email": email,
            "offerId": offer_id,
            "paymentProvider": "PAY2ME",
            "currency": CURRENCY,
            "paymentMethod": "SBP",
        }
    else:
        endpoint = f"{LAVA_BASE}/api/v2/invoice"
        body = {
            "email": email,
            "offerId": offer_id,
            "periodicity": t["periodicity"],
            "currency": CURRENCY,
            "paymentMethod": PAYMENT_METHOD,
            "buyerLanguage": "RU",
            "amount": invoice_price,
            "clientUtm": client_utm,
        }

    try:
        async with session.post(
            endpoint,
            headers={
                "X-Api-Key": LAVA_API_KEY,
                "accept": "application/json",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        ) as resp:
            text = await resp.text()
            if resp.status not in (200, 201):
                log.error("lava /invoice -> %s | тело ответа: %s", resp.status, text)
                return None
            data = json.loads(text)
            url = data.get("paymentUrl") or data.get("paymentURL") or data.get("url")
            if not url:
                log.error("В ответе нет ссылки на оплату: %s", text)
            return url
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка при создании счёта: %s", e)
        return None


# ---------------------------------------------------------------------------
# База данных (учёт сроков подписки на тариф "месяц")
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
            status TEXT
        )''')
        try:
            conn.run("ALTER TABLE members ADD COLUMN IF NOT EXISTS reminded_stage INTEGER DEFAULT 0")
        except Exception as _e:  # noqa: BLE001
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
        conn.run('''CREATE TABLE IF NOT EXISTS payment_attempts (
            email TEXT PRIMARY KEY,
            user_id BIGINT,
            username TEXT,
            tariff TEXT,
            created_at TIMESTAMP
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
        log.info("База готова, таблицы members, consents, pending_members, blacklisted_users, payment_attempts, yoomoney_attempts на месте.")
    except Exception as e:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001
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
        log.info("Импорт: в базу %s, в ожидание %s (всего %s)",
                 to_members, to_pending, to_members + to_pending)
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка импорта: %s", e)


def save_consent(user_id: int, username: str):
    log.info("Согласие с офертой: user_id=%s username=%s", user_id, username)
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run(
            "INSERT INTO consents (user_id, username, accepted_at) "
            "VALUES (:uid, :un, :ts)",
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


def save_pending_payment(email: str, user_id: int, username: str, tariff_key: str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run(
            '''INSERT INTO payment_attempts (email, user_id, username, tariff, created_at)
               VALUES (:email, :uid, :un, :tf, :ts)
               ON CONFLICT (email) DO UPDATE SET
               user_id=:uid, username=:un, tariff=:tf, created_at=:ts''',
            email=email, uid=user_id, un=username or "—", tf=tariff_key, ts=datetime.now(),
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("Не смог сохранить ожидающую оплату %s/%s: %s", email, user_id, e)


def load_pending_payment(email: str) -> Optional[dict]:
    if not DATABASE_URL:
        return None
    try:
        conn = get_db()
        rows = conn.run(
            "SELECT user_id, username, tariff FROM payment_attempts WHERE email=:email",
            email=email,
        )
        conn.close()
        if not rows:
            return None
        user_id, username, tariff = rows[0]
        return {"user_id": user_id, "username": username, "tariff": tariff}
    except Exception as e:  # noqa: BLE001
        log.error("Не смог прочитать ожидающую оплату %s: %s", email, e)
        return None


def delete_pending_payment(email: str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run("DELETE FROM payment_attempts WHERE email=:email", email=email)
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("Не смог удалить ожидающую оплату %s: %s", email, e)


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
                    log.info("Истёк и уже не в группе, убран тихо: @%s (%s)", username, user_id)
                    continue
                log.error("Ошибка кика %s: %s", user_id, e)
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ <b>Не удалось удалить участника</b>\n\n"
                        f"Пользователь: @{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Причина: {e}\n\n"
                        f"Проверь, что бот — администратор клуба с правом банить.\n"
                        f"Можно убрать вручную: /remove {user_id}",
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
            log.info("Кикнут по истечении срока: %s", user_id)
            if kicked_ok:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚪 <b>Участник удалён по окончании подписки</b>\n\n"
                        f"Пользователь: @{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Подписка закончилась: {end_dt.strftime('%d.%m.%Y')}",
                    )
                except Exception as e2:  # noqa: BLE001
                    log.error("Не смог уведомить админа о кике %s: %s", user_id, e2)


# --- Напоминания об окончании подписки ---

REMIND_3_DAYS = (
    "Привет 🤍 Через 3 дня твой доступ в Creator Lab заканчивается.\n\n"
    "Напомню, что останется за дверью, если уйти:\n"
    "🎬 Новые материалы каждую неделю — а на этой как раз свежие\n"
    "🤖 Готовые GPT-агенты и сервисы в одном месте\n"
    "💬 Чат, где можно спросить и получить живой ответ\n"
    "💸 Методы, за которые другие платят тысячами\n\n"
    "Продли заранее и оставайся с нами 👇"
)

REMIND_1_DAY = (
    "Привет 🤍 Завтра твой доступ в Creator Lab закрывается.\n\n"
    "Будет жаль расставаться именно сейчас — когда всё освоено и пошёл "
    "результат. Внутри ждут новые материалы, сервисы и люди, которые "
    "занимаются тем же, что и ты.\n\n"
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
        log.error("check_reminders: не смог прочитать базу: %s", e)
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
                log.info("Напоминание за 1 день отправлено: %s", user_id)
            except Exception as e:  # noqa: BLE001
                log.error("Не смог отправить напоминание (1д) %s: %s", user_id, e)

        elif 1 < days_left <= 3 and reminded == 0:
            try:
                await bot.send_message(user_id, REMIND_3_DAYS, reply_markup=renew_keyboard())
                c = get_db()
                c.run("UPDATE members SET reminded_stage=3 WHERE user_id=:uid", uid=user_id)
                c.close()
                log.info("Напоминание за 3 дня отправлено: %s", user_id)
            except Exception as e:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001
        return f"Не смог прочитать базу: {e}"

    forever_n = forever[0][0] if forever else 0
    expired_n = expired[0][0] if expired else 0

    lines = [
        "✅ <b>Я на посту и слежу за подписками</b>\n",
        f"👥 Активных месячных: {len(active)}",
        f"💎 Навсегда: {forever_n}",
        f"🚪 Уже удалённых (истёкших): {expired_n}\n",
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
        if len(active) > 10:
            lines.append(f"…и ещё {len(active) - 10}")
    else:
        lines.append("Активных месячных подписок пока нет.")

    return "\n".join(lines)


async def daily_report(bot: Bot):
    try:
        today = datetime.now().strftime("%d.%m.%Y")
        text = await build_status_text()
        await bot.send_message(
            ADMIN_ID, f"🌙 <b>Итог дня — {today}</b>\n\n" + text)
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отправить ежедневный отчёт: %s", e)


# ---------------------------------------------------------------------------
# Хендлеры бота
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
    "Это правила доступа к клубу Creator Lab. Если коротко: все материалы "
    "защищены авторским правом. Их нельзя копировать, распространять или "
    "продавать. За нарушение — удаление из клуба и блокировка без возврата "
    "средств.\n\n"
    "Нажимая «Принимаю условия», ты подтверждаешь, что прочитал(а) оферту "
    "и согласен(на) с ней."
)


def accept_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Принимаю условия", callback_data="accept_oferta")],
        ]
    )


WARNING_TEXT = (
    "⚠️ <b>ВАЖНО. Прочти перед оплатой.</b>\n\n"
    "Все материалы Creator Lab — видео, промпты, методики, шаблоны — это моя "
    "интеллектуальная собственность.\n\n"
    "🚫 Если ты заходишь в клуб, чтобы скопировать материалы и использовать их "
    "в своём продукте, курсе или клубе — вход для тебя закрыт. Не оплачивай.\n\n"
    "При обнаружении факта копирования, перепродажи или использования моих "
    "материалов доступ будет закрыт без возврата средств, а также я оставляю "
    "за собой право на юридические действия.\n\n"
    "Все эти условия закреплены в договоре-оферте. Нажимая «Я соглашаюсь», "
    "ты подтверждаешь согласие с её условиями."
)


def warning_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Я соглашаюсь", callback_data="accept_warning")],
        ]
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = await build_status_text()
    await message.answer(text)


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


def _find_blacklist_entry(user_id: Optional[int] = None, username: Optional[str] = None):
    if not DATABASE_URL:
        return None
    uname = _clean_username(username or "") if username else None
    conn = None
    try:
        conn = get_db()
        if user_id is not None and uname:
            rows = conn.run(
                "SELECT id, user_id, username FROM blacklisted_users "
                "WHERE user_id=:uid OR LOWER(username)=LOWER(:un) LIMIT 1",
                uid=user_id, un=uname,
            )
        elif user_id is not None:
            rows = conn.run(
                "SELECT id, user_id, username FROM blacklisted_users "
                "WHERE user_id=:uid LIMIT 1",
                uid=user_id,
            )
        elif uname:
            rows = conn.run(
                "SELECT id, user_id, username FROM blacklisted_users "
                "WHERE LOWER(username)=LOWER(:un) LIMIT 1",
                un=uname,
            )
        else:
            rows = []

        if rows and user_id is not None:
            row_id, db_uid, db_username = rows[0]
            if db_uid is None or (uname and not db_username):
                conn.run(
                    "UPDATE blacklisted_users "
                    "SET user_id=COALESCE(user_id, :uid), "
                    "username=COALESCE(username, :un) WHERE id=:bid",
                    uid=user_id, un=uname, bid=row_id,
                )
        return rows[0] if rows else None
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка проверки черного списка: %s", e)
        return None
    finally:
        if conn:
            conn.close()


def _is_blacklisted(user_id: int, username: Optional[str]) -> bool:
    return _find_blacklist_entry(user_id=user_id, username=username) is not None


def _add_to_blacklist(user_id: Optional[int], username: Optional[str]):
    if not DATABASE_URL:
        raise RuntimeError("База не подключена.")
    uname = _clean_username(username or "") if username else None
    conn = get_db()
    try:
        existing = []
        if uname:
            existing = conn.run(
                "SELECT id FROM blacklisted_users WHERE LOWER(username)=LOWER(:un) LIMIT 1",
                un=uname,
            )
        if existing:
            conn.run(
                "UPDATE blacklisted_users SET "
                "user_id=COALESCE(:uid, user_id), username=COALESCE(:un, username), "
                "added_at=:ts WHERE id=:bid",
                uid=user_id, un=uname, ts=datetime.now(), bid=existing[0][0],
            )
        elif user_id is not None:
            conn.run(
                "INSERT INTO blacklisted_users (user_id, username, added_at) "
                "VALUES (:uid, :un, :ts) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "username=COALESCE(EXCLUDED.username, blacklisted_users.username), "
                "added_at=EXCLUDED.added_at",
                uid=user_id, un=uname, ts=datetime.now(),
            )
        elif uname:
            conn.run(
                "INSERT INTO blacklisted_users (user_id, username, added_at) "
                "VALUES (NULL, :un, :ts)",
                un=uname, ts=datetime.now(),
            )
    finally:
        conn.close()


def _remove_from_blacklist(user_id: Optional[int], username: Optional[str]) -> int:
    if not DATABASE_URL:
        raise RuntimeError("База не подключена.")
    uname = _clean_username(username or "") if username else None
    conn = get_db()
    try:
        if user_id is not None and uname:
            rows = conn.run(
                "SELECT id FROM blacklisted_users "
                "WHERE user_id=:uid OR LOWER(username)=LOWER(:un)",
                uid=user_id, un=uname,
            )
        elif user_id is not None:
            rows = conn.run("SELECT id FROM blacklisted_users WHERE user_id=:uid", uid=user_id)
        elif uname:
            rows = conn.run(
                "SELECT id FROM blacklisted_users WHERE LOWER(username)=LOWER(:un)",
                un=uname,
            )
        else:
            rows = []
        for row in rows:
            conn.run("DELETE FROM blacklisted_users WHERE id=:bid", bid=row[0])
        return len(rows)
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
        "Выбери кнопку ниже, чтобы установить любую цену:",
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
        f"Напиши новую цену в рублях для тарифа <b>«{TARIFFS[tariff_key]['name']}»</b> (только число):"
    )
    await cb.answer()


@router.message(StateFilter(AdminPrice.waiting_for_price))
async def on_new_price_input(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    raw_price = (message.text or "").strip()
    if not raw_price.isdigit() or int(raw_price) <= 0:
        await message.answer("Пожалуйста, отправь корректное число больше нуля. Например: 790")
        return

    new_price = int(raw_price)
    data = await state.get_data()
    tariff_key = data.get("tariff")
    if tariff_key in TARIFFS:
        TARIFFS[tariff_key]["price"] = new_price
        await state.clear()
        await message.answer(
            f"✅ Цена тарифа «{TARIFFS[tariff_key]['name']}» успешно обновлена на <b>{new_price}₽</b>!\n\n"
            f"<i>(Если бот перезапустится на Render, вернутся цены из переменных окружения PRICE_MONTH/PRICE_FOREVER)</i>",
            reply_markup=admin_keyboard(),
        )
    else:
        await state.clear()
        await message.answer("Ошибка: тариф не найден. Нажми /admin заново.")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[1] not in ("month", "forever") or not parts[2].isdigit():
        await message.answer(
            "Использование команды:\n"
            "<code>/setprice month 699</code>\n"
            "<code>/setprice forever 9990</code>\n\n"
            "Или просто используй меню: /admin"
        )
        return
    tariff_key, price = parts[1], int(parts[2])
    TARIFFS[tariff_key]["price"] = price
    await message.answer(f"✅ Цена тарифа «{TARIFFS[tariff_key]['name']}» изменена на <b>{price}₽</b>.")


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "<b>Цены и тарифы:</b>\n"
        "/admin — открыть панель изменения цен (кнопками)\n"
        "/setprice month 699 — быстро изменить цену месяца\n"
        "/setprice forever 9990 — быстро изменить цену навсегда\n\n"
        "<b>Просмотр:</b>\n"
        "/status — краткая сводка (на посту ли я)\n"
        "/members — полный список участников со сроками\n"
        "/find @username или /find id — найти человека\n"
        "/stats — статистика продаж и участников\n\n"
        "<b>Ручное управление</b> (для оплат мимо бота — крипта, старые ссылки):\n"
        "/add @username 30 — добавить на 30 дней\n"
        "/add 6882319264 30 — добавить по id\n"
        "/add @username forever — добавить навсегда\n"
        "/extend @username 30 — продлить на 30 дней\n"
        "/remove @username — кикнуть из клуба и убрать из учёта\n"
        "/remove 6882319264 — кикнуть по id (кого угодно)\n"
        "/ban @username или /ban id — добавить в черный список и запретить оплату\n"
        "/unban @username или /unban id — снять из черного списка\n\n"
        "<b>Рассылка:</b>\n"
        "/sale 899 — отправить предложение купить 1 месяц со скидкой\n\n"
        "<b>VIP (вечный доступ + иммунитет от кика):</b>\n"
        "/vip @username или /vip id — сделать VIP\n"
        "/unvip @username — снять VIP-статус\n\n"
        "<b>Обслуживание базы:</b>\n"
        "/sync — сверить базу с группой и убрать тех, кто вышел\n"
        "/reimport — восстановить всех из файла (откат, если /sync ошибся)\n"
        "/cleanup — должники с кнопками «убрать / продлить»\n"
        "/purge — разом убрать ВСЕХ просроченных\n\n"
        "💡 Везде можно указывать либо @username, либо числовой id. "
        "По id можно добавить или кикнуть даже того, кого нет в базе."
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
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка чтения базы: {e}")
        return

    if not rows:
        await message.answer("В базе пока нет участников.")
        return

    active_month = []
    vip = []
    expired = []
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
            lines.append(
                f"{i}. {mark} @{username or '—'} — до {end_dt.strftime('%d.%m.%Y')} "
                f"(ост. {days_left} дн.)"
            )

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
            rows = conn.run(
                "SELECT user_id, username, tariff, start_dt, end_dt, status FROM members "
                "WHERE user_id=:uid", uid=uid_in)
        else:
            rows = conn.run(
                "SELECT user_id, username, tariff, start_dt, end_dt, status FROM members "
                "WHERE LOWER(username)=LOWER(:un)", un=uname)
        conn.close()
    except Exception as e:  # noqa: BLE001
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
        active_month = conn.run(
            "SELECT COUNT(*) FROM members WHERE status='active' AND end_dt IS NOT NULL")[0][0]
        forever = conn.run(
            "SELECT COUNT(*) FROM members WHERE status='active' AND end_dt IS NULL")[0][0]
        expired = conn.run("SELECT COUNT(*) FROM members WHERE status='expired'")[0][0]
        conn.close()
    except Exception as e:  # noqa: BLE001
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
        rows = conn.run(
            "SELECT user_id, username, tariff FROM members WHERE status='active'")
        conn.close()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка чтения базы: {e}")
        return

    ghosts = []
    checked = 0
    errors = 0

    for user_id, username, tariff in rows:
        checked += 1
        try:
            member = await bot.get_chat_member(CLUB_CHAT_ID, user_id)
            if member.status in ("left", "kicked"):
                ghosts.append((user_id, username, tariff))
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("sync: не смог проверить @%s (id %s): %s", username, user_id, e)
        await asyncio.sleep(0.05)

    if not ghosts:
        msg = f"✅ Готово! Проверено {checked}, все на месте. Призраков нет 🎉"
        if errors:
            msg += f"\n\n(⚠️ {errors} не смог проверить — их не трогал, оставил в базе)"
        await message.answer(msg)
        return

    try:
        conn = get_db()
        for user_id, _u, _t in ghosts:
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=user_id)
        conn.close()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Нашёл призраков, но не смог обновить базу: {e}")
        return

    lines = [f"🧹 <b>Синхронизация завершена</b>\n",
             f"Проверено: {checked}",
             f"Убрано призраков (нет в группе): {len(ghosts)}\n",
             "<b>Кого убрал из учёта:</b>"]
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
        await message.answer(f"Файл {path} не найден в репозитории.")
        return
    await message.answer("🔄 Перечитываю список из файла и восстанавливаю базу…")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка восстановления: {e}")
        return
    await message.answer(
        f"✅ Восстановлено {restored} участников из файла.\n"
        f"Все вернулись в активные с правильными сроками. Проверь /status."
    )


@router.message(Command("sale"))
async def cmd_sale(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /sale 899")
        return
    try:
        sale_price = int(parts[1].strip())
    except ValueError:
        await message.answer("Цена должна быть числом. Например: /sale 899")
        return
    if sale_price <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return
    if not DATABASE_URL:
        await message.answer("База не подключена, я не смогу найти пользователей для рассылки.")
        return

    try:
        conn = get_db()
        rows = conn.run(
            "SELECT DISTINCT ON (c.user_id) c.user_id, NULLIF(NULLIF(c.username, '—'), '') "
            "FROM consents c "
            "LEFT JOIN members m ON m.user_id=c.user_id AND m.status='active' "
            "LEFT JOIN blacklisted_users b ON b.user_id=c.user_id OR "
            "(NULLIF(NULLIF(c.username, '—'), '') IS NOT NULL "
            "AND b.username IS NOT NULL "
            "AND LOWER(b.username)=LOWER(NULLIF(NULLIF(c.username, '—'), ''))) "
            "WHERE c.user_id IS NOT NULL AND m.user_id IS NULL AND b.id IS NULL "
            "ORDER BY c.user_id, c.accepted_at DESC"
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка чтения базы для рассылки: {e}")
        return

    if not rows:
        await message.answer("Некому отправлять: не нашла пользователей без активного доступа.")
        return

    await message.answer(
        f"Начинаю рассылку предложения на 1 месяц за {sale_price}₽. "
        f"Получателей: {len(rows)}."
    )

    sale_text = (
        "Привет! Сейчас можно попасть в Creator Lab со скидкой 💛\n\n"
        f"1 месяц доступа — {sale_price}₽.\n"
        "Внутри клуба материалы, промпты, сервисы и поддержка для контента с нейросетями.\n\n"
        "Нажми кнопку ниже, если хочешь забрать доступ со скидкой."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"Забрать доступ за {sale_price}₽",
                callback_data=f"sale_month:{sale_price}",
            )
        ]]
    )

    sent = 0
    failed = 0
    for user_id, _username in rows:
        try:
            await bot.send_message(user_id, sale_text, reply_markup=kb)
            sent += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.warning("sale: не смог отправить %s: %s", user_id, e)
        await asyncio.sleep(0.05)

    await message.answer(
        f"✅ Рассылка завершена.\nОтправлено: {sent}\nНе отправилось: {failed}"
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 and not message.forward_from:
        await message.answer(
            "Использование:\n"
            "/ban @username — запретить оплату по нику\n"
            "/ban 6882319264 — запретить оплату по id\n"
            "Можно также переслать сообщение пользователя и написать /ban."
        )
        return

    uid = None
    uname = None
    if len(parts) >= 2:
        uid, uname = _parse_target(parts[1])
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname
    if uid is None and uname:
        found_uid, found_uname = await _lookup_in_db(username=uname)
        if found_uid:
            uid = found_uid
            uname = found_uname
    if uid is None and not uname:
        await message.answer("Не вижу, кого добавить в черный список.")
        return

    try:
        _add_to_blacklist(uid, uname)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка черного списка: {e}")
        return

    name_show = ("@" + uname) if uname else f"id {uid}"
    await message.answer(
        f"🚫 {name_show} добавлен(а) в черный список. Оплату через бота он(а) сделать не сможет.\n"
        "Пользователь увидит уведомление только при попытке купить доступ через бота."
    )


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 and not message.forward_from:
        await message.answer("Использование: /unban @username или /unban 6882319264")
        return

    uid = None
    uname = None
    if len(parts) >= 2:
        uid, uname = _parse_target(parts[1])
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname
    if uid is None and uname:
        found_uid, found_uname = await _lookup_in_db(username=uname)
        if found_uid:
            uid = found_uid
            uname = found_uname
    if uid is None and not uname:
        await message.answer("Не вижу, кого снять с черного списка.")
        return

    try:
        removed = _remove_from_blacklist(uid, uname)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка черного списка: {e}")
        return

    name_show = ("@" + uname) if uname else f"id {uid}"
    if removed:
        await message.answer(f"✅ {name_show} снят(а) с черного списка. Оплата снова доступна.")
    else:
        await message.answer(f"{name_show} не найден(а) в черном списке.")


@router.message(Command("add"))
async def cmd_add(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Использование:\n"
            "/add @username 30 — на 30 дней\n"
            "/add 6882319264 30 — по id\n"
            "/add @username forever — навсегда"
        )
        return
    target = parts[1]
    term = parts[2].lower()

    uid, uname = _parse_target(target)
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname

    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer(
            f"Не знаю id для @{uname} 🤔 Укажи числовой id "
            f"(/add 6882319264 {term}) или перешли мне его сообщение."
        )
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
            await message.answer("Дату пиши так: 16.07.2026 (день.месяц.год).")
            return
        tariff = "month"
    else:
        try:
            days = int(term)
        except ValueError:
            await message.answer(
                "Срок: число дней (26), дата (16.07.2026) или слово forever.")
            return
        end_dt = start_dt + timedelta(days=days)
        tariff = "month"

    name_show = ("@" + uname) if uname else f"id {uid}"
    save_member(uid, uname or "—", tariff, start_dt, end_dt)
    if end_dt:
        await message.answer(
            f"✅ Добавлен(а) {name_show} на {term} дн.\n"
            f"Доступ: {start_dt.strftime('%d.%m.%Y')} → {end_dt.strftime('%d.%m.%Y')}\n"
            f"Теперь я сам(а) буду следить — напомню и кикну по сроку."
        )
    else:
        await message.answer(f"✅ Добавлен(а) {name_show} навсегда 💎")


@router.message(Command("vip"))
async def cmd_vip(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "/vip @username — сделать VIP (вечный доступ + иммунитет)\n"
            "/vip 6882319264 — по id"
        )
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
        await message.answer(
            f"Не знаю id для @{uname} 🤔 Укажи числовой id (/vip 6882319264) "
            f"или перешли мне его сообщение."
        )
        return

    save_member(uid, uname or "—", "vip", datetime.now(), None)
    name_show = ("@" + uname) if uname else f"id {uid}"
    await message.answer(
        f"👑 {name_show} теперь VIP!\n"
        f"Вечный доступ, иммунитет от кика, не считается в продажах. "
        f"Случайно удалить через /remove не получится — только через /unvip."
    )


@router.message(Command("unvip"))
async def cmd_unvip(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /unvip @username или /unvip 6882319264")
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
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid AND tariff='vip'",
                     uid=uid)
            conn.close()
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Ошибка: {e}")
            return
    name_show = ("@" + uname) if uname else f"id {uid}"
    await message.answer(f"VIP-статус снят с {name_show}. Теперь его можно удалить через /remove.")


@router.message(Command("extend"))
async def cmd_extend(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Использование:\n/extend @username 30\n/extend 6882319264 30 (по id)")
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
            rows = conn.run(
                "SELECT user_id, end_dt, username FROM members WHERE user_id=:uid", uid=uid_in)
        else:
            rows = conn.run(
                "SELECT user_id, end_dt, username FROM members WHERE LOWER(username)=LOWER(:un)",
                un=uname)
        if not rows:
            conn.close()
            who = f"id {uid_in}" if uid_in is not None else f"@{uname}"
            await message.answer(f"{who} не найден. Сначала добавь через /add.")
            return
        user_id, end_dt, db_name = rows[0]
        base = end_dt if (end_dt and end_dt > now) else now
        new_end = base + timedelta(days=days)
        conn.run(
            "UPDATE members SET end_dt=:ed, status='active', reminded_stage=0 WHERE user_id=:uid",
            ed=new_end, uid=user_id)
        conn.close()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка: {e}")
        return
    name_show = ("@" + db_name) if db_name else f"id {user_id}"
    await message.answer(
        f"✅ Продлено {name_show} на {days} дн.\nНовый срок до: {new_end.strftime('%d.%m.%Y')}"
    )


@router.message(Command("remove"))
async def cmd_remove(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование:\n"
            "/remove @username — по нику\n"
            "/remove 6882319264 — по id (можно кикнуть кого угодно)"
        )
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
        await message.answer(
            f"Не нашёл @{uname} в базе. Укажи числовой id "
            f"(/remove 6882319264), чтобы кикнуть кого угодно."
        )
        return

    if DATABASE_URL:
        try:
            conn = get_db()
            vip = conn.run(
                "SELECT tariff FROM members WHERE user_id=:uid AND tariff='vip' AND status='active'",
                uid=uid)
            conn.close()
            if vip:
                await message.answer(
                    "👑 Это VIP-участник, его нельзя удалить через /remove.\n"
                    "Если точно нужно — сначала сними статус командой /unvip, потом /remove."
                )
                return
        except Exception:  # noqa: BLE001
            pass

    if DATABASE_URL:
        try:
            conn = get_db()
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=uid)
            conn.close()
        except Exception as e:  # noqa: BLE001
            log.error("remove: ошибка базы для %s: %s", uid, e)

    name_show = ("@" + uname) if uname else f"id {uid}"
    try:
        await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await message.answer(f"✅ {name_show} удалён(а) из клуба и убран(а) из учёта.")
    except Exception as e:  # noqa: BLE001
        await message.answer(
            f"⚠️ {name_show}: из учёта убран(а), но кикнуть не вышло: {e}\n"
            f"(возможно, человека нет в клубе или нет прав)"
        )


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
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка: {e}")
        return

    if not rows:
        await message.answer("✅ Должников нет — все активные в сроке 🎉")
        return

    await message.answer(
        f"⏰ <b>У этих закончился срок ({len(rows)}):</b>\n"
        "Жми «Убрать» напротив тех, кого выгнать из учёта."
    )
    for user_id, username, end_dt in rows:
        d = end_dt.strftime("%d.%m.%Y") if end_dt else "—"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🗑 Убрать", callback_data=f"kick:{user_id}"),
            InlineKeyboardButton(
                text="💛 +30 дней", callback_data=f"ext30:{user_id}"),
        ]])
        await message.answer(
            f"@{username or '—'} (id <code>{user_id}</code>) — истёк {d}",
            reply_markup=kb)


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
        rows = conn.run(
            "SELECT user_id, username FROM members "
            "WHERE status='active' AND end_dt IS NOT NULL AND end_dt < :now", now=now)
        conn.close()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка: {e}")
        return

    if not rows:
        await message.answer("✅ Просроченных нет — всё чисто 🎉")
        return

    await message.answer(f"🧹 Убираю {len(rows)} просроченных, минутку…")
    kicked = 0
    archived = 0
    for user_id, username in rows:
        try:
            conn = get_db()
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=user_id)
            conn.close()
            archived += 1
        except Exception:  # noqa: BLE001
            pass
        try:
            await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
            await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
            kicked += 1
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)

    await message.answer(
        f"✅ Готово!\n"
        f"Убрано из учёта: {archived}\n"
        f"Реально кикнуто из клуба: {kicked}\n"
        f"(остальных в группе уже не было)\n\n"
        f"Проверь /status — просроченные исчезли."
    )


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
        except Exception:  # noqa: BLE001
            pass
    note = "убран из учёта"
    try:
        await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        note = "удалён из клуба и убран из учёта"
    except Exception:  # noqa: BLE001
        note = "убран из учёта (в группе его уже не было)"
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
            conn.run(
                "UPDATE members SET end_dt=:ed, status='active', reminded_stage=0 "
                "WHERE user_id=:uid", ed=new_end, uid=uid)
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    await cb.message.edit_text(
        f"💛 Продлён на 30 дней — до {new_end.strftime('%d.%m.%Y')}.")
    await cb.answer("Продлено")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    try:
        await message.answer_document(
            FSInputFile(OFERTA_PATH, filename="Договор-оферта Creator Lab.pdf"),
            caption=OFERTA_TEXT,
            reply_markup=accept_keyboard(),
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отправить PDF оферты: %s", e)
        await message.answer(OFERTA_TEXT, reply_markup=accept_keyboard())


@router.callback_query(F.data == "accept_oferta")
async def on_accept(cb: CallbackQuery, state: FSMContext):
    user = cb.from_user
    save_consent(user.id, user.username)
    await cb.message.answer(WARNING_TEXT, reply_markup=warning_keyboard())
    await cb.answer("Спасибо! Условия приняты ✅")


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
    await cb.message.answer(
        f"Отлично, оформляем доступ на 1 месяц со скидкой за {sale_price}₽.\n\n" + ASK_EMAIL
    )
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
    await cb.message.answer(
        "Отлично, продлеваем доступ на месяц 💛\n\n" + ASK_EMAIL
    )
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

    await state.update_data(email=email)
    await state.set_state(Buy.waiting_method)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ЮMoney / банковская карта 💳", callback_data="pay_yoomoney")],
        ]
    )
    await message.answer(
        "Отлично! Теперь выбери, чем удобнее оплатить 👇\n\n"
        "📅 После оплаты я покажу срок доступа, а для тарифа на месяц "
        "заранее напомню об окончании, чтобы ты успел(а) продлить 🤍",
        reply_markup=kb,
    )


async def _make_payment(cb: CallbackQuery, state: FSMContext,
                        lava_session: ClientSession, method: str):
    user = cb.from_user
    data = await state.get_data()
    email = data.get("email")
    tariff_key = data.get("tariff")
    sale_price = data.get("sale_price")
    price_override = int(sale_price) if sale_price else None
    if not email or not tariff_key:
        await cb.message.answer("Кажется, сессия сбросилась. Нажми /start и начни заново 🙂")
        await state.clear()
        await cb.answer()
        return
    if _is_blacklisted(user.id, user.username):
        await cb.message.answer(BLACKLIST_NOTICE)
        await state.clear()
        await cb.answer()
        return

    if method == "yoomoney":
        if not YOOMONEY_RECEIVER or not YOOMONEY_NOTIFICATION_SECRET:
            await cb.message.answer(
                "Оплата через ЮMoney пока не настроена 😔 "
                "Напиши, пожалуйста, @adelin_creator."
            )
            await state.clear()
            await cb.answer()
            return

        price = price_override or TARIFFS[tariff_key]["price"]
        label = make_yoomoney_label(user.id)
        save_yoomoney_attempt(label, email, user.id, user.username, tariff_key, price)
        await state.clear()

        url = f"{PUBLIC_BASE_URL}/yoomoney/pay/{label}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате 💳", url=url)]]
        )
        await cb.message.answer(
            f"Готово! Нажми кнопку ниже и оплати доступ ({price}₽) через ЮMoney.\n\n"
            "Как только оплата пройдёт, я пришлю тебе ссылку в клуб прямо сюда.",
            reply_markup=kb,
        )
        await cb.answer()
        return

    await cb.message.answer("Секунду, создаю для тебя ссылку на оплату… ⏳")
    url = await create_invoice(
        lava_session, email, user.id, user.username, tariff_key,
        method=method, price_override=price_override,
    )

    if not url:
        await cb.message.answer(
            "Что-то пошло не так при создании оплаты 😔 "
            "Напиши, пожалуйста, @adelin_creator, я разберусь."
        )
        await state.clear()
        await cb.answer()
        return

    pending[email] = {"user_id": user.id, "username": user.username, "tariff": tariff_key}
    save_pending_payment(email, user.id, user.username, tariff_key)
    await state.clear()

    price = price_override or TARIFFS[tariff_key]["price"]
    how = "через СБП" if method == "sbp" else "картой"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате 💳", url=url)]]
    )
    await cb.message.answer(
        f"Готово! Нажми кнопку ниже и оплати доступ ({price}₽) {how}.\n\n"
        "Как только оплата пройдёт, я <b>сразу</b> пришлю тебе ссылку в клуб прямо сюда. "
        "Закрывать чат не нужно 🙂",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data == "pay_card", StateFilter(Buy.waiting_method))
async def on_pay_card(cb: CallbackQuery, state: FSMContext, lava_session: ClientSession):
    await _make_payment(cb, state, lava_session, method="card")


@router.callback_query(F.data == "pay_yoomoney", StateFilter(Buy.waiting_method))
async def on_pay_yoomoney(cb: CallbackQuery, state: FSMContext, lava_session: ClientSession):
    await _make_payment(cb, state, lava_session, method="yoomoney")


@router.callback_query(F.data == "pay_sbp", StateFilter(Buy.waiting_method))
async def on_pay_sbp(cb: CallbackQuery, state: FSMContext, lava_session: ClientSession):
    await _make_payment(cb, state, lava_session, method="sbp")


# ---------------------------------------------------------------------------
# Выдача доступа
# ---------------------------------------------------------------------------

async def grant_access(bot: Bot, email: str, amount, currency, contract_id: str,
                       tariff_name: str = "—", tariff_key: Optional[str] = None,
                       user_id: Optional[int] = None, username: Optional[str] = None):
    if not user_id:
        info = pending.get(email)
        if not info:
            info = load_pending_payment(email)
        if info:
            user_id = info["user_id"]
            username = info.get("username")
            if info.get("tariff") in TARIFFS:
                tariff_key = info["tariff"]
                tariff_name = TARIFFS[info["tariff"]]["name"]

    if not user_id:
        log.warning("Оплата по %s есть, но покупатель не опознан", email)
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Пришла оплата на почту <b>{email}</b> ({amount} {currency}), "
            f"но я не смог опознать покупателя автоматически.\n"
            f"Контракт: {contract_id}\nВыдай доступ вручную, пожалуйста.",
        )
        return

    username = username or "—"

    if _is_blacklisted(user_id, username):
        try:
            await bot.send_message(user_id, BLACKLIST_NOTICE)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог отправить уведомление из черного списка %s: %s", user_id, e)
        await bot.send_message(
            ADMIN_ID,
            f"🚫 Оплата от @{username} (id <code>{user_id}</code>, {email}) пришла, "
            f"но доступ не выдан: пользователь в черном списке.",
        )
        pending.pop(email, None)
        delete_pending_payment(email)
        return

    try:
        link = await bot.create_chat_invite_link(
            chat_id=CLUB_CHAT_ID,
            member_limit=1,
            name=f"buyer {user_id}",
        )
        invite_url = link.invite_link
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог создать ссылку-приглашение: %s", e)
        await bot.send_message(
            ADMIN_ID,
            f"❗️ Оплата прошла (@{username}, {email}), но я не смог создать ссылку в клуб: {e}\n"
            f"Проверь, что бот — админ клуба с правом приглашать. Выдай доступ вручную.",
        )
        return

    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=30) if tariff_key == "month" else None

    if end_dt:
        period_line = (
            f"📅 Твой доступ активен с {start_dt.strftime('%d.%m.%Y')} "
            f"по {end_dt.strftime('%d.%m.%Y')}.\n"
            "Я заранее напомню тебе об окончании — пришлю сообщение за 3 дня "
            "и за 1 день, чтобы ты успел(а) продлить без перерыва 🤍\n\n"
        )
    else:
        period_line = "📅 Твой доступ — навсегда, без ограничения по сроку 💎\n\n"

    delivered_ok = False
    try:
        await bot.send_message(
            user_id,
            "Оплата получена, спасибо! 💛\n\n"
            "Добро пожаловать в Creator Lab 🔐\n\n"
            f"{period_line}"
            "Вот твоя личная ссылка в клуб (одноразовая, действует для тебя):\n"
            f"{invite_url}\n\n"
            "Заходи и пользуйся 🚀",
        )
        delivered_ok = True
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог отправить ссылку покупателю: %s", e)

    if not delivered_ok:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ <b>Оплата прошла, но ссылка клиенту НЕ доставлена</b>\n\n"
            f"Покупатель: @{username} (id <code>{user_id}</code>)\n"
            f"Почта: {email}\n"
            f"Сумма: {amount} {currency}\n"
            f"Контракт: {contract_id}\n"
            f"Причина обычно такая: пользователь заблокировал бота или не нажал /start.\n\n"
            f"Ссылка для ручной отправки:\n{invite_url}",
        )
        pending.pop(email, None)
        delete_pending_payment(email)
        return

    save_member(user_id, username, tariff_key or "—", start_dt, end_dt)

    if end_dt:
        access_str = f"с {start_dt.strftime('%d.%m.%Y')} по {end_dt.strftime('%d.%m.%Y')}"
    else:
        access_str = "бессрочно (навсегда)"

    await bot.send_message(
        ADMIN_ID,
        f"💰 <b>Новая продажа!</b>\n\n"
        f"Тариф: {tariff_name}\n"
        f"Покупатель: @{username} (id <code>{user_id}</code>)\n"
        f"Почта: {email}\n"
        f"Сумма: {amount} {currency}\n"
        f"Контракт: {contract_id}\n"
        f"Доступ: {access_str}",
    )

    pending.pop(email, None)
    delete_pending_payment(email)


# ---------------------------------------------------------------------------
# ЮMoney кошелек: форма оплаты + HTTP-уведомления
# ---------------------------------------------------------------------------

def make_yoomoney_label(user_id: int) -> str:
    return f"ym_{user_id}_{uuid.uuid4().hex[:12]}"


def verify_yoomoney_sign(params: dict[str, str]) -> bool:
    """
    Проверка sha1_hash из HTTP-уведомлений ЮMoney.
    Формула официального протокола ЮMoney:
    sha1(notification_type&operation_id&amount&currency&datetime&sender&codepro&notification_secret&label)
    """
    if not YOOMONEY_NOTIFICATION_SECRET:
        log.error("YOOMONEY_NOTIFICATION_SECRET не задан")
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
    success_url = f"{PUBLIC_BASE_URL}/"
    targets = f"Creator Lab: {tariff_name}"

    fields = {
        "receiver": YOOMONEY_RECEIVER,
        "quickpay-form": "button",
        "paymentType": "AC",
        "sum": str(amount),
        "label": label,
        "targets": targets,
        "successURL": success_url,
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
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Оплата Creator Lab</title>
</head>
<body>
  <p>Переходим к оплате...</p>
  <form id="pay" method="POST" action="https://yoomoney.ru/quickpay/confirm">
    {inputs}
    <button type="submit">Перейти к оплате</button>
  </form>
  <script>document.getElementById('pay').submit();</script>
</body>
</html>"""
    return web.Response(text=page, content_type="text/html")


async def handle_yoomoney_notification(request: web.Request) -> web.Response:
    secret = request.match_info.get("secret")
    if secret is not None and secret != WEBHOOK_SECRET:
        return web.Response(status=404, text="not found")

    form = await request.post()
    params = {k: str(v) for k, v in form.items()}
    log.info("Уведомление ЮMoney: %s", json.dumps(params, ensure_ascii=False)[:2000])

    if not params:
        log.info("ЮMoney: получено пустое тестовое уведомление")
        return web.Response(text="test ok")

    if not verify_yoomoney_sign(params):
        log.warning("ЮMoney: неверная подпись sha1_hash")
        return web.Response(status=403, text="bad sign")
    if params.get("unaccepted") == "true" or params.get("codepro") == "true":
        log.warning("ЮMoney: перевод не принят или защищен кодом, label=%s", params.get("label"))
        return web.Response(text="ignored")

    label = params.get("label") or ""
    info = load_yoomoney_attempt(label)
    if not info:
        log.warning("ЮMoney: неизвестный label %s", label)
        await request.app["bot"].send_message(
            ADMIN_ID,
            f"⚠️ Пришла оплата ЮMoney с неизвестной меткой <code>{label}</code>.\n"
            f"Сумма: {params.get('withdraw_amount') or params.get('amount')} RUB\n"
            f"Операция: {params.get('operation_id')}\n"
            f"Проверь вручную в кошельке ЮMoney.",
        )
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
        log.warning("ЮMoney: сумма меньше ожидаемой %s < %s, label=%s", paid, expected, label)
        await request.app["bot"].send_message(
            ADMIN_ID,
            f"⚠️ ЮMoney: сумма меньше ожидаемой.\n"
            f"Ожидалось: {expected} RUB\nПолучено/списано: {paid_raw} RUB\n"
            f"Label: <code>{label}</code>",
        )
        return web.Response(text="ignored")

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
    return web.Response(text="ok")


# ---------------------------------------------------------------------------
# Веб-сервер: вебхук лавы + пинг для UptimeRobot
# ---------------------------------------------------------------------------

async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def handle_yoomoney_notification_check(request: web.Request) -> web.Response:
    return web.Response(text="YOOMONEY NOTIFICATION ENDPOINT OK")


async def handle_webhook(request: web.Request) -> web.Response:
    if request.match_info.get("secret") != WEBHOOK_SECRET:
        return web.Response(status=404, text="not found")

    raw = await request.text()
    log.info("Вебхук от лавы: %s", raw[:2000])
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="bad json")

    event = (data.get("eventType") or "").lower()
    status = (data.get("status") or "").lower()
    buyer = data.get("buyer") or {}
    email = (buyer.get("email") or "").strip().lower()
    amount = data.get("amount")
    currency = data.get("currency") or CURRENCY
    contract_id = data.get("contractId") or ""
    product = data.get("product") or {}
    product_id = product.get("id")

    utm = data.get("clientUtm") or {}
    utm_user_id = None
    utm_username = None
    tariff_name = "—"
    tariff_key = None
    try:
        if utm.get("utm_content"):
            utm_user_id = int(str(utm["utm_content"]).strip())
        if utm.get("utm_term"):
            utm_username = str(utm["utm_term"]).strip() or None
        camp = str(utm.get("utm_campaign") or "").strip()
        if camp in TARIFFS:
            tariff_key = camp
            tariff_name = TARIFFS[camp]["name"]
    except (ValueError, TypeError):
        utm_user_id = None

    if not tariff_key and product_id and product_id in PRODUCT_TO_TARIFF:
        tariff_key = PRODUCT_TO_TARIFF[product_id]
        tariff_name = TARIFFS[tariff_key]["name"]

    bot: Bot = request.app["bot"]

    success = event == "payment.success" and status in (
        "completed", "active", "paid", "subscription-active",
    )
    if not success:
        return web.json_response({"status": "ignored"})

    if contract_id and contract_id in processed_contracts:
        return web.json_response({"status": "duplicate"})
    if contract_id:
        processed_contracts.add(contract_id)

    if product_id and product_id not in PRODUCT_TO_TARIFF:
        log.warning("Оплата по неизвестному продукту %s, пропускаю", product_id)
        return web.json_response({"status": "wrong product"})

    if not email:
        log.warning("В вебхуке нет почты покупателя")
        return web.json_response({"status": "no email"})

    await grant_access(
        bot, email, amount, currency, contract_id,
        tariff_name=tariff_name, tariff_key=tariff_key,
        user_id=utm_user_id, username=utm_username,
    )
    return web.json_response({"status": "ok"})


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
            conn.run("UPDATE members SET username=:un WHERE user_id=:uid",
                     un=uname, uid=user.id)
            conn.close()
            return
        pend = conn.run(
            "SELECT tariff, end_dt FROM pending_members WHERE LOWER(username)=LOWER(:un)",
            un=uname)
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
    except Exception as e:  # noqa: BLE001
        log.error("collect_id_from_group ошибка для @%s: %s", uname, e)


async def on_startup_checks(bot: Bot, session: ClientSession):
    import_pending_from_json()
    if LAVA_API_KEY:
        for key, t in TARIFFS.items():
            await resolve_offer_id(session, key)
            if t["product_id"]:
                PRODUCT_TO_TARIFF[t["product_id"]] = key
        log.info("Карта продуктов lava: %s", PRODUCT_TO_TARIFF)
    else:
        log.info("Lava отключена: LAVA_API_KEY не задан, продукты lava не загружаем.")

    try:
        chat = await bot.get_chat(CLUB_CHAT_ID)
        log.info("Клуб найден: %s (id %s)", chat.title, CLUB_CHAT_ID)
        me = await bot.get_me()
        member = await bot.get_chat_member(CLUB_CHAT_ID, me.id)
        log.info("Статус бота в клубе: %s", member.status)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "Не вижу клуб %s или бот там не админ: %s. "
            "Проверь CLUB_CHAT_ID и что бот добавлен админом с правом приглашать.",
            CLUB_CHAT_ID, e,
        )


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в переменных окружения")
    if not YOOMONEY_RECEIVER or not YOOMONEY_NOTIFICATION_SECRET:
        log.warning(
            "ЮMoney настроен не полностью: нужны YOOMONEY_RECEIVER и "
            "YOOMONEY_NOTIFICATION_SECRET."
        )
    if not WEBHOOK_SECRET or WEBHOOK_SECRET == "change-me-please":
        log.warning("WEBHOOK_SECRET лучше задать своим секретным значением в Render.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    session = ClientSession()
    dp["lava_session"] = session

    init_db()
    await on_startup_checks(bot, session)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expired, "interval", hours=1, args=[bot])
    scheduler.add_job(check_reminders, "interval", hours=12, args=[bot])
    scheduler.add_job(daily_report, CronTrigger(hour=20, minute=0), args=[bot])
    scheduler.start()
    log.info("Планировщик кика запущен (проверка раз в час).")

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/yoomoney/pay/{label}", handle_yoomoney_pay)
    app.router.add_get("/yoomoney/notification", handle_yoomoney_notification_check)
    app.router.add_get("/yoomoney/notification/", handle_yoomoney_notification_check)
    app.router.add_get("/yoomoney/notification/{secret}", handle_yoomoney_notification_check)
    app.router.add_get("/yoomoney/notification/{secret}/", handle_yoomoney_notification_check)
    app.router.add_post("/yoomoney/notification", handle_yoomoney_notification)
    app.router.add_post("/yoomoney/notification/", handle_yoomoney_notification)
    app.router.add_post("/yoomoney/notification/{secret}", handle_yoomoney_notification)
    app.router.add_post("/yoomoney/notification/{secret}/", handle_yoomoney_notification)
    app.router.add_post("/yoomoney/{secret}", handle_yoomoney_notification)
    app.router.add_post("/yoomoney/{secret}/", handle_yoomoney_notification)
    app.router.add_post("/lava/{secret}", handle_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(
        "Веб-сервер слушает порт %s. ЮMoney: /yoomoney/pay/<label>, "
        "уведомления: /yoomoney/notification/<secret>",
        PORT,
    )

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
