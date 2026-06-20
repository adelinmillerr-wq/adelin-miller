# -*- coding: utf-8 -*-
"""
Бот доступа в клуб Creator Lab с оплатой через lava.top.

Два тарифа:
  - month   : подписка на 1 месяц
  - forever : разовая покупка навсегда

Логика:
  1. /start -> приветствие + кнопки выбора тарифа.
  2. Бот просит почту (на неё придёт чек) и сохраняет username.
  3. Бот спрашивает способ оплаты (карта / СБП).
  4. Бот создаёт счёт в lava.top -> получает ссылку на оплату.
  5. После оплаты lava.top стучится на /lava/<secret> (вебхук).
  6. Бот опознаёт покупателя (по метке utm_content = telegram-id),
     выдаёт одноразовую ссылку в клуб и шлёт уведомление админу.

Все секреты берутся из переменных окружения (Render -> Environment).
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Any

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

CLUB_CHAT_ID = int(os.getenv("CLUB_CHAT_ID", "-1003973853516"))  # id клуба (с -100)
ADMIN_ID = int(os.getenv("ADMIN_ID", "1619432734"))             # кому слать уведомления

DATABASE_URL = os.getenv("DATABASE_URL", "")  # PostgreSQL на Render — для учёта сроков подписки

# секрет в адресе вебхука (/lava/<secret>)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me-please")

PORT = int(os.getenv("PORT", "10000"))
LAVA_BASE = "https://gate.lava.top"

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
    "💎 Хочешь оплатить криптой? Напиши мне в личку @adelin_miller — подскажу реквизиты\n\n"
    "Если возникли проблемы с оплатой — напиши @adelin_miller"
)

ASK_EMAIL = (
    "Напиши, пожалуйста, свою почту 📧\n"
    "На неё лава пришлёт чек об оплате. Просто отправь её сообщением."
)

ASK_USERNAME = (
    "Чтобы я смог(ла) выдать тебе доступ, поставь, пожалуйста, "
    "<b>username</b> в настройках Telegram (Настройки → Имя пользователя), "
    "а потом нажми кнопку ещё раз 🙂"
)

BAD_EMAIL = "Хм, это не похоже на почту 🤔 Пришли в формате name@mail.ru, пожалуйста."

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


async def resolve_offer_id(session: ClientSession, tariff_key: str) -> str:
    """offerId для тарифа: из env, либо ищет по продукту, либо берёт product_id."""
    if tariff_key in resolved_offers:
        return resolved_offers[tariff_key]

    t = TARIFFS[tariff_key]

    if t["offer_id"]:
        resolved_offers[tariff_key] = t["offer_id"]
        log.info("[%s] offerId из env: %s", tariff_key, t["offer_id"])
        return t["offer_id"]

    # пробуем достать из списка продуктов
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

    # запасной вариант
    resolved_offers[tariff_key] = t["product_id"]
    log.info("[%s] используем product_id как offerId: %s", tariff_key, t["product_id"])
    return t["product_id"]


async def create_invoice(
    session: ClientSession, email: str, user_id: int, username: str,
    tariff_key: str, method: str = "card",
) -> Optional[str]:
    """Создаёт счёт в lava.top и возвращает ссылку на оплату."""
    t = TARIFFS[tariff_key]
    offer_id = await resolve_offer_id(session, tariff_key)

    # метки, по которым потом опознаём покупателя и тариф (вернутся в вебхуке)
    client_utm = {
        "utm_source": "tgbot",
        "utm_content": str(user_id),
        "utm_term": username or "",
        "utm_campaign": tariff_key,
    }

    if method == "sbp":
        # СБП: строго по примеру поддержки lava — только эти поля, без лишнего.
        # (buyerLanguage и clientUtm лава для v3/СБП не принимает -> "Restricted payment method type")
        endpoint = f"{LAVA_BASE}/api/v3/invoice"
        body = {
            "email": email,
            "offerId": offer_id,
            "paymentProvider": "PAY2ME",
            "currency": CURRENCY,
            "paymentMethod": "SBP",
        }
    else:
        # Карта: через v2
        endpoint = f"{LAVA_BASE}/api/v2/invoice"
        body = {
            "email": email,
            "offerId": offer_id,
            "periodicity": t["periodicity"],
            "currency": CURRENCY,
            "paymentMethod": PAYMENT_METHOD,
            "buyerLanguage": "RU",
            "amount": t["price"],
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
    # postgres://user:pass@host:port/dbname
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
        # колонка для отметки, какое напоминание уже отправлено (0=нет, 3=за 3 дня, 1=за 1 день)
        # ALTER в try, чтобы не падать если колонка уже есть (старая база)
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
        # таблица ожидания: участники из Excel, у которых пока нет user_id.
        # слушатель группы найдёт их по username и перенесёт в members с настоящим id.
        conn.run('''CREATE TABLE IF NOT EXISTS pending_members (
            username TEXT PRIMARY KEY,
            tariff TEXT,
            end_dt TIMESTAMP
        )''')
        conn.close()
        log.info("База готова, таблицы members, consents, pending_members на месте.")
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка инициализации базы: %s", e)


def import_pending_from_json():
    """Один раз загружает участников из members_import.json.
    Есть id → сразу в members (бот видит и кикает). Нет id → в ожидание (pending),
    слушатель группы допишет id потом. Повторный запуск не дублирует."""
    if not DATABASE_URL:
        return
    path = os.getenv("IMPORT_FILE", "members_import.json")
    if not os.path.exists(path):
        return
    try:
        import json
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

            if uid:  # есть id — заносим прямо в основную базу
                # не перезаписываем тех, кто уже активен (например, недавно купил сам)
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
            else:  # id нет — в ожидание, слушатель допишет
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
    """Фиксируем согласие с офертой — доказательство акцепта."""
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
    """Сохраняем покупателя. end_dt=None для тарифа 'навсегда'."""
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


async def check_expired(bot: Bot):
    """Раз в час: кикаем тех, у кого срок (end_dt) вышел. 'навсегда' (end_dt=NULL) не трогаем."""
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
            try:
                # ban + unban = выкинуть, но оставить возможность вернуться
                await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
                await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
                await bot.send_message(
                    user_id,
                    "Твоя подписка на месяц закончилась, доступ в клуб закрыт 🤍\n\n"
                    "Будем рады видеть снова — нажми /start, чтобы продлить.",
                )
                c = get_db()
                c.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=user_id)
                c.close()
                log.info("Кикнут по истечении срока: %s", user_id)
                # уведомление админу, что кик реально сработал
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚪 <b>Участник удалён по окончании подписки</b>\n\n"
                        f"Пользователь: @{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Подписка закончилась: {end_dt.strftime('%d.%m.%Y')}",
                    )
                except Exception as e2:  # noqa: BLE001
                    log.error("Не смог уведомить админа о кике %s: %s", user_id, e2)
            except Exception as e:  # noqa: BLE001
                log.error("Ошибка кика %s: %s", user_id, e)
                # сообщаем админу, что кикнуть НЕ удалось (например, нет прав)
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ <b>Не удалось удалить участника</b>\n\n"
                        f"Пользователь: @{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Причина: {e}\n\n"
                        f"Проверь, что бот — администратор клуба с правом банить.",
                    )
                except Exception:  # noqa: BLE001
                    pass


# --- Напоминания об окончании подписки (за 3 дня и за 1 день) ---

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
    """Раз в день: шлём напоминания тем, у кого месяц заканчивается через 3 и 1 день.
    Только тариф month (у 'навсегда' end_dt=NULL). Каждое напоминание — один раз."""
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
        days_left = (end_dt - now).total_seconds() / 86400  # сколько суток осталось
        reminded = reminded or 0

        # за 1 день (от 0 до 1 суток) — приоритетнее, шлём если ещё не слали "1"
        if 0 < days_left <= 1 and reminded != 1:
            try:
                await bot.send_message(user_id, REMIND_1_DAY, reply_markup=renew_keyboard())
                c = get_db()
                c.run("UPDATE members SET reminded_stage=1 WHERE user_id=:uid", uid=user_id)
                c.close()
                log.info("Напоминание за 1 день отправлено: %s", user_id)
            except Exception as e:  # noqa: BLE001
                log.error("Не смог отправить напоминание (1д) %s: %s", user_id, e)

        # за 3 дня (от 1 до 3 суток) — шлём если ещё ничего не слали (reminded==0)
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
    """Собирает сводку по подпискам для отчёта админу / команды /status."""
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
        for username, end_dt in active[:10]:  # первые 10 по дате
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
    """Вечерний итог дня для админа — общая статистика по клубу."""
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


OFERTA_PATH = os.getenv("OFERTA_PATH", "oferta.pdf")  # PDF лежит рядом с bot.py

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


@router.message(Command("status"))
async def cmd_status(message: Message):
    # отвечаем только администратору (тебе)
    if message.from_user.id != ADMIN_ID:
        return
    text = await build_status_text()
    await message.answer(text)


# ===================== АДМИН-ПАНЕЛЬ =====================

def _is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


def _clean_username(raw: str) -> str:
    """Убираем @ и пробелы из ника."""
    return raw.strip().lstrip("@").strip()


def _parse_target(raw: str):
    """Распознаёт, что ввёл админ: числовой id или @username.
    Возвращает (user_id|None, username|None)."""
    t = raw.strip().lstrip("@").strip()
    if t.lstrip("-").isdigit():   # это числовой id
        return int(t), None
    return None, t                # это ник


async def _lookup_in_db(user_id=None, username=None):
    """Ищет участника в базе по id или нику. Возвращает (user_id, username) или (None, None)."""
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


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
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
        "/remove 6882319264 — кикнуть по id (кого угодно)\n\n"
        "💡 Везде можно указывать либо @username, либо числовой id. "
        "По id можно добавить или кикнуть даже того, кого нет в базе."
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
            "ORDER BY status, end_dt NULLS LAST"
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка чтения базы: {e}")
        return

    if not rows:
        await message.answer("В базе пока нет участников.")
        return

    # формируем список, разбивая на части (Telegram лимит ~4096 символов)
    lines = ["👥 <b>Участники клуба</b>\n"]
    for username, tariff, start_dt, end_dt, status in rows:
        if status == "expired":
            mark = "❌"
            period = f"истёк {end_dt.strftime('%d.%m.%Y')}" if end_dt else "истёк"
        elif end_dt is None:
            mark = "💎"
            period = f"навсегда (с {start_dt.strftime('%d.%m.%Y')})" if start_dt else "навсегда"
        else:
            days_left = int((end_dt - now).total_seconds() / 86400)
            mark = "⚠️" if days_left <= 3 else "✅"
            s = start_dt.strftime('%d.%m.%Y') if start_dt else "—"
            period = f"{s} → {end_dt.strftime('%d.%m.%Y')} (ост. {days_left} дн.)"
        lines.append(f"{mark} @{username or '—'} — {period}")

    # отправляем кусками по 50 строк
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

    # распознаём id или ник
    uid, uname = _parse_target(target)
    # если переслано сообщение — берём id оттуда (приоритет)
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname

    # если дали ник — пробуем найти id в базе
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
    else:
        try:
            days = int(term)
        except ValueError:
            await message.answer("Срок должен быть числом дней или словом forever.")
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

    # VIP = тариф 'vip', без даты окончания → бот никогда не кикнет
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
        # продлеваем от текущей даты окончания или от сегодня, если уже истёк
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
    # если переслано сообщение — берём id оттуда
    if message.forward_from:
        uid = message.forward_from.id
        uname = message.forward_from.username or uname

    # если дали ник — ищем id в базе
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

    # защита: VIP нельзя удалить случайно
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

    # помечаем в базе как истёкшего (если есть)
    if DATABASE_URL:
        try:
            conn = get_db()
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=uid)
            conn.close()
        except Exception as e:  # noqa: BLE001
            log.error("remove: ошибка базы для %s: %s", uid, e)

    # кикаем из клуба
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


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    # Отправляем PDF-оферту + кнопку «Принимаю»
    try:
        await message.answer_document(
            FSInputFile(OFERTA_PATH, filename="Договор-оферта Creator Lab.pdf"),
            caption=OFERTA_TEXT,
            reply_markup=accept_keyboard(),
        )
    except Exception as e:  # noqa: BLE001
        # если файл не нашёлся — не блокируем человека, показываем текст
        logger.error("Не смог отправить PDF оферты: %s", e)
        await message.answer(OFERTA_TEXT, reply_markup=accept_keyboard())


@router.callback_query(F.data == "accept_oferta")
async def on_accept(cb: CallbackQuery, state: FSMContext):
    user = cb.from_user
    # фиксируем согласие в базе (доказательство)
    save_consent(user.id, user.username)
    await cb.message.answer(GREETING, reply_markup=tariff_keyboard())
    await cb.answer("Спасибо! Условия приняты ✅")


@router.callback_query(F.data.in_({"tariff_month", "tariff_forever"}))
async def on_tariff(cb: CallbackQuery, state: FSMContext):
    user = cb.from_user
    if not user.username:
        await cb.message.answer(ASK_USERNAME)
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
    """Кнопка 'Продлить доступ' из напоминания — запускает оплату месяца."""
    user = cb.from_user
    if not user.username:
        await cb.message.answer(ASK_USERNAME)
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
            [InlineKeyboardButton(text="Банковская карта 💳", callback_data="pay_card")],
            [InlineKeyboardButton(text="СБП (по QR / номеру) 📲", callback_data="pay_sbp")],
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
    if not email or not tariff_key:
        await cb.message.answer("Кажется, сессия сбросилась. Нажми /start и начни заново 🙂")
        await state.clear()
        await cb.answer()
        return

    await cb.message.answer("Секунду, создаю для тебя ссылку на оплату… ⏳")
    url = await create_invoice(
        lava_session, email, user.id, user.username, tariff_key, method=method
    )

    if not url:
        await cb.message.answer(
            "Что-то пошло не так при создании оплаты 😔 "
            "Напиши, пожалуйста, @adelin_miller, я разберусь."
        )
        await state.clear()
        await cb.answer()
        return

    pending[email] = {"user_id": user.id, "username": user.username, "tariff": tariff_key}
    await state.clear()

    price = TARIFFS[tariff_key]["price"]
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

    # считаем срок заранее, чтобы показать его и клиенту, и в учёте
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
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог отправить ссылку покупателю: %s", e)

    # запись в базу для учёта срока
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


# ---------------------------------------------------------------------------
# Веб-сервер: вебхук лавы + пинг для UptimeRobot
# ---------------------------------------------------------------------------

async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def handle_webhook(request: web.Request) -> web.Response:
    # Защита — секретный адрес /lava/<secret>.
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

    # надёжный способ узнать тариф (работает и для СБП, где меток нет): по product_id
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

    # сверяем, что это один из наших продуктов
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
    """Слушатель клуба: ловит user_id активных участников и переносит
    тех, кто ждёт в pending_members (из Excel), в основную базу с настоящим id."""
    if not DATABASE_URL or not message.from_user:
        return
    user = message.from_user
    if not user.username:
        return  # без username не сопоставить со списком
    uname = user.username
    try:
        conn = get_db()
        # уже в основной базе? обновим username на актуальный и выйдем
        in_main = conn.run("SELECT 1 FROM members WHERE user_id=:uid", uid=user.id)
        if in_main:
            conn.run("UPDATE members SET username=:un WHERE user_id=:uid",
                     un=uname, uid=user.id)
            conn.close()
            return
        # ждёт в pending по username?
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
    # импорт участников из Excel-файла в таблицу ожидания (один раз, без дублей)
    import_pending_from_json()
    # резолвим оферы и строим карту product_id -> тариф
    for key, t in TARIFFS.items():
        await resolve_offer_id(session, key)
        if t["product_id"]:
            PRODUCT_TO_TARIFF[t["product_id"]] = key
    log.info("Карта продуктов: %s", PRODUCT_TO_TARIFF)

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
    if not BOT_TOKEN or not LAVA_API_KEY:
        raise SystemExit("Не заданы BOT_TOKEN и/или LAVA_API_KEY в переменных окружения")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    session = ClientSession()
    dp["lava_session"] = session

    init_db()
    await on_startup_checks(bot, session)

    # планировщик: раз в час проверяем, у кого истёк месяц, и кикаем
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expired, "interval", hours=1, args=[bot])
    scheduler.add_job(check_reminders, "interval", hours=12, args=[bot])
    # ежедневный отчёт в 23:00 по Москве. Render работает по UTC, МСК = UTC+3,
    # поэтому 23:00 МСК = 20:00 UTC. Если у тебя другой пояс — поменяй hour ниже.
    scheduler.add_job(daily_report, CronTrigger(hour=20, minute=0), args=[bot])
    scheduler.start()
    log.info("Планировщик кика запущен (проверка раз в час).")

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_post("/lava/{secret}", handle_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Веб-сервер слушает порт %s. Вебхук: /lava/<secret>", PORT)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
