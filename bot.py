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

from aiohttp import web, ClientSession

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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
    "Если возникли проблемы с оплатой — напиши @adelin_pro"
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
        conn.close()
        log.info("База готова, таблица members на месте.")
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка инициализации базы: %s", e)


def save_member(user_id: int, username: str, tariff_key: str,
                start_dt: datetime, end_dt: Optional[datetime]):
    """Сохраняем покупателя. end_dt=None для тарифа 'навсегда'."""
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run(
            '''INSERT INTO members (user_id, username, tariff, start_dt, end_dt, status)
               VALUES (:uid, :un, :tf, :sd, :ed, 'active')
               ON CONFLICT (user_id) DO UPDATE SET
               username=:un, tariff=:tf, start_dt=:sd, end_dt=:ed, status='active' ''',
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
            "SELECT user_id, end_dt FROM members WHERE end_dt IS NOT NULL AND status='active'"
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("check_expired: не смог прочитать базу: %s", e)
        return

    for user_id, end_dt in rows:
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
            except Exception as e:  # noqa: BLE001
                log.error("Ошибка кика %s: %s", user_id, e)


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
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(GREETING, reply_markup=tariff_keyboard())


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
    await message.answer("Отлично! Теперь выбери, чем удобнее оплатить 👇", reply_markup=kb)


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
            "Напиши, пожалуйста, @adelin_pro, я разберусь."
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

    try:
        await bot.send_message(
            user_id,
            "Оплата получена, спасибо! 💛\n\n"
            "Добро пожаловать в Creator Lab 🔐\n\n"
            "Вот твоя личная ссылка в клуб (одноразовая, действует для тебя):\n"
            f"{invite_url}\n\n"
            "Заходи и пользуйся 🚀",
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог отправить ссылку покупателю: %s", e)

    # запись в базу для учёта срока. Месяц -> +30 дней, навсегда -> без срока (None)
    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=30) if tariff_key == "month" else None
    save_member(user_id, username, tariff_key or "—", start_dt, end_dt)

    await bot.send_message(
        ADMIN_ID,
        f"💰 <b>Новая продажа!</b>\n\n"
        f"Тариф: {tariff_name}\n"
        f"Покупатель: @{username} (id <code>{user_id}</code>)\n"
        f"Почта: {email}\n"
        f"Сумма: {amount} {currency}\n"
        f"Контракт: {contract_id}",
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

async def on_startup_checks(bot: Bot, session: ClientSession):
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
