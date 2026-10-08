# -*- coding: utf-8 -*-
"""
Creator Lab access bot (режим "только уборка").

Оплата через бота ОТКЛЮЧЕНА. Новые участники приходят через Tribute.
Бот продолжает:
  - удалять из клуба тех, кто купил доступ через него раньше, когда срок закончился;
  - слать напоминания об окончании (со ссылкой на Tribute);
  - давать админу команды управления (/admin, /find, /extend, /remove, /ban ...);
  - при каждом запуске отзывать все старые инвайт-ссылки, которые создавал сам.
"""

import os
import hmac
import hashlib
import asyncio
import logging
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

# Ссылка на оформление подписки через Tribute. Задай в Render -> Environment.
TRIBUTE_URL = os.getenv("TRIBUTE_URL", "https://t.me/tribute/app?startapp=sYrT").strip()

# Секрет ЮMoney нужен только чтобы узнавать о "поздних" платежах и сообщать админу.
YOOMONEY_NOTIFICATION_SECRET = os.getenv("YOOMONEY_NOTIFICATION_SECRET", "").strip()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("club-bot")


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------

GREETING = (
    "Привет! 🔐\n\n"
    "Creator Lab — закрытый клуб для тех, кто делает контент с помощью нейросетей.\n\n"
    "Внутри:\n"
    "🎬 Фото, видео, монтаж — от идеи до готового контента\n"
    "🤖 Собственные GPT-агенты — обученные, готовые к работе\n"
    "💸 Киношные видео в Seedance 2.0\n"
    "🛠 Все актуальные сервисы — в одном месте\n"
    "💬 Уютный чат поддержки — живые люди, живые ответы\n"
    "🎁 Новые материалы каждую неделю\n\n"
    "Подписку на клуб теперь можно оформить через <b>Tribute</b> 👇\n\n"
    "Если возникли вопросы, напиши @adelin_creator"
)

GREETING_NO_LINK = (
    "Привет! 🔐\n\n"
    "Creator Lab — закрытый клуб для тех, кто делает контент с помощью нейросетей.\n\n"
    "Оплата в этом боте больше не принимается. Подписку на клуб теперь можно "
    "оформить через Tribute. Ссылку пришлёт @adelin_creator."
)


class AdminPrice(StatesGroup):
    # Оставлено, чтобы не ломать старые состояния. Цены больше не используются.
    waiting_for_price = State()


class AdminAsk(StatesGroup):
    waiting = State()


router = Router()


def tribute_keyboard() -> Optional[InlineKeyboardMarkup]:
    if not TRIBUTE_URL:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💛 Оформить подписку", url=TRIBUTE_URL)]]
    )


def greeting_text() -> str:
    return GREETING if TRIBUTE_URL else GREETING_NO_LINK


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
    return user, password, parsed.hostname, int(parsed.port or 5432), parsed.path.lstrip("/")


def get_db():
    user, password, host, port, dbname = parse_db_url(DATABASE_URL)
    return pg8000.native.Connection(
        user=user, password=password, host=host, port=port, database=dbname
    )


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
            """CREATE TABLE IF NOT EXISTS blacklisted_users (
                id SERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE,
                username TEXT,
                added_at TIMESTAMP
            )"""
        )
        conn.run(
            """CREATE TABLE IF NOT EXISTS invite_links (
                id SERIAL PRIMARY KEY,
                label TEXT,
                user_id BIGINT,
                invite_link TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                revoked BOOLEAN NOT NULL DEFAULT FALSE,
                joined_user_id BIGINT,
                joined_at TIMESTAMP,
                source TEXT
            )"""
        )

        # Таблицу yoomoney_attempts бот больше не пишет, но она нужна для истории
        # и для /resend. Если её нет, создаём пустую.
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

        for sql in (
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS in_chat BOOLEAN DEFAULT FALSE",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS chat_status TEXT",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP",
        ):
            conn.run(sql)

        # Старые ссылки из yoomoney_attempts попадают в invite_links, чтобы их можно было отозвать.
        conn.run(
            """INSERT INTO invite_links (label, user_id, invite_link, created_at, revoked, source)
               SELECT ya.label, ya.user_id, ya.invite_link,
                      COALESCE(ya.paid_at, ya.created_at, NOW()), FALSE, 'migration'
               FROM yoomoney_attempts ya
               WHERE ya.invite_link IS NOT NULL AND ya.invite_link <> ''
               ON CONFLICT (invite_link) DO NOTHING"""
        )

        conn.run("CREATE INDEX IF NOT EXISTS idx_members_status ON members(status)")
        conn.run("CREATE INDEX IF NOT EXISTS idx_members_username_lower ON members(LOWER(username))")
        log.info("База данных готова к работе.")
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка инициализации базы: %s", e)
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
        r = rows[0]
        return {
            "user_id": r[0], "username": r[1], "tariff": r[2], "start_dt": r[3],
            "end_dt": r[4], "status": r[5], "reminded_stage": r[6] or 0,
            "in_chat": bool(r[7]), "chat_status": r[8], "last_synced_at": r[9],
        }
    except Exception as e:  # noqa: BLE001
        log.error("Не смог прочитать участника %s: %s", user_id, e)
        return None
    finally:
        if conn:
            conn.close()


def save_member(user_id: int, username: str, tariff_key: str,
                start_dt: datetime, end_dt: Optional[datetime]):
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
            uid=user_id, un=username, tf=tariff_key, sd=start_dt, ed=end_dt,
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
            """UPDATE members SET in_chat=:inch, chat_status=:status, last_synced_at=:ts
               WHERE user_id=:uid""",
            uid=user_id, inch=in_chat, status=chat_status, ts=datetime.now(),
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог обновить присутствие %s: %s", user_id, e)
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
            """UPDATE yoomoney_attempts SET access_granted=TRUE, invite_link=:invite_link
               WHERE label=:label""",
            label=label, invite_link=invite_link,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог сохранить invite link %s: %s", label, e)
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Invite links
# ---------------------------------------------------------------------------


def save_invite_link(label: str, user_id: int, invite_link: str, source: str = "resend"):
    if not DATABASE_URL or not invite_link:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            """INSERT INTO invite_links (label, user_id, invite_link, created_at, revoked, source)
               VALUES (:label, :uid, :link, :ts, FALSE, :source)
               ON CONFLICT (invite_link) DO NOTHING""",
            label=label, uid=user_id, link=invite_link, ts=datetime.now(), source=source,
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог сохранить invite link %s: %s", invite_link, e)
    finally:
        if conn:
            conn.close()


def mark_invite_revoked(invite_link: str, joined_user_id: Optional[int] = None):
    if not DATABASE_URL or not invite_link:
        return
    conn = None
    try:
        conn = get_db()
        conn.run(
            """UPDATE invite_links
               SET revoked=TRUE,
                   joined_user_id=COALESCE(:joined_uid, joined_user_id),
                   joined_at=CASE WHEN :joined_uid IS NULL THEN joined_at ELSE :ts END
               WHERE invite_link=:link""",
            link=invite_link, joined_uid=joined_user_id, ts=datetime.now(),
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отметить invite link отозванной: %s", e)
    finally:
        if conn:
            conn.close()


async def revoke_invite_link(bot: Bot, invite_link: str, joined_user_id: Optional[int] = None) -> bool:
    if not invite_link:
        return False
    try:
        await bot.revoke_chat_invite_link(chat_id=CLUB_CHAT_ID, invite_link=invite_link)
        mark_invite_revoked(invite_link, joined_user_id)
        return True
    except Exception as e:  # noqa: BLE001
        err = str(e).lower()
        if any(m in err for m in (
            "invite_hash_expired", "invite link is expired",
            "invite link has expired", "already revoked",
        )):
            mark_invite_revoked(invite_link, joined_user_id)
            return True
        log.warning("Не удалось отозвать invite link %s: %s", invite_link, e)
        return False


async def revoke_all_bot_invites(bot: Bot) -> dict:
    result = {"found": 0, "revoked": 0, "failed": 0}
    if not DATABASE_URL:
        return result

    conn = None
    try:
        conn = get_db()
        rows = conn.run("SELECT invite_link FROM invite_links WHERE revoked=FALSE ORDER BY id")
    except Exception as e:  # noqa: BLE001
        log.error("Не смог получить invite links для очистки: %s", e)
        return result
    finally:
        if conn:
            conn.close()

    result["found"] = len(rows)
    for (invite_link,) in rows:
        if await revoke_invite_link(bot, invite_link):
            result["revoked"] += 1
        else:
            result["failed"] += 1
        await asyncio.sleep(0.05)
    return result


# ---------------------------------------------------------------------------
# Telegram presence / sync
# ---------------------------------------------------------------------------


async def get_chat_presence(bot: Bot, user_id: int):
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
        if any(m in err for m in (
            "participant_id_invalid", "user_not_participant",
            "member not found", "user not found", "user_id_invalid",
        )):
            return False, "left"
        return None, f"error:{type(e).__name__}"


async def sync_members(bot: Bot) -> dict:
    result = {"db_active": 0, "in_chat": 0, "absent": [], "errors": [], "group_total": None}
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
            """SELECT user_id, username, tariff, end_dt FROM members
               WHERE status='active' ORDER BY end_dt NULLS LAST"""
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
                "user_id": user_id, "username": username, "tariff": tariff,
                "end_dt": end_dt, "chat_status": tg_status,
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
        f"💳 Активных в базе бота: <b>{data['db_active']}</b>",
        f"✅ Из них сейчас в группе: <b>{data['in_chat']}</b>",
        f"⚠️ В базе активны, но не в группе: <b>{len(data['absent'])}</b>",
    ]
    if data["absent"]:
        lines += ["", "<b>Нет в группе:</b>"]
        for item in data["absent"][:50]:
            end_dt = item["end_dt"]
            until = f"до {end_dt.strftime('%d.%m.%Y')}" if end_dt else "навсегда"
            lines.append(f"• @{item['username'] or '—'} (<code>{item['user_id']}</code>) — {until}")
        if len(data["absent"]) > 50:
            lines.append(f"… ещё {len(data['absent']) - 50}")
    if data["errors"]:
        lines += ["", f"❗ Ошибок проверки: <b>{len(data['errors'])}</b>"]
    return "\n".join(lines)


async def background_sync(bot: Bot):
    try:
        data = await sync_members(bot)
        log.info(
            "Фоновая синхронизация: DB=%s, в группе=%s, отсутствуют=%s, ошибок=%s",
            data["db_active"], data["in_chat"], len(data["absent"]), len(data["errors"]),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка фоновой синхронизации: %s", e)


@router.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated, bot: Bot):
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

    if in_chat and event.invite_link:
        invite_url = getattr(event.invite_link, "invite_link", None)
        creator = getattr(event.invite_link, "creator", None)
        if invite_url and creator and getattr(creator, "id", None) == bot.id:
            await revoke_invite_link(bot, invite_url, joined_user_id=user_id)


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
            """SELECT user_id, end_dt, username FROM members
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
                    f"@{username or '—'} (id <code>{user_id}</code>)\nTelegram: {tg_status}",
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
                        f"@{username or '—'} (id <code>{user_id}</code>)\nПричина: {e}",
                    )
                except Exception:
                    pass
                continue

        conn = None
        try:
            conn = get_db()
            conn.run(
                """UPDATE members SET status='expired', in_chat=FALSE, chat_status=:cs
                   WHERE user_id=:uid""",
                uid=int(user_id), cs=tg_status or "left",
            )
        except Exception:
            pass
        finally:
            if conn:
                conn.close()

        text = "Твоя подписка закончилась, доступ в клуб закрыт 🤍\n\n"
        if TRIBUTE_URL:
            text += "Будем рады видеть тебя снова. Продлить можно через Tribute 👇"
        else:
            text += "Будем рады видеть тебя снова. Напиши @adelin_creator, пришлю ссылку на Tribute."
        try:
            await bot.send_message(int(user_id), text, reply_markup=tribute_keyboard())
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
    "Чтобы остаться с нами, оформи подписку через Tribute 👇"
)
REMIND_1_DAY = (
    "Привет 🤍 Завтра твой доступ в Creator Lab закрывается.\n\n"
    "Продлить можно через Tribute 👇 Будем рады видеть тебя дальше 💛"
)


async def check_reminders(bot: Bot):
    if not DATABASE_URL:
        return

    now = datetime.now()
    conn = None
    try:
        conn = get_db()
        rows = conn.run(
            """SELECT user_id, end_dt, reminded_stage FROM members
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

        stage = None
        text = None
        if 0 < days_left <= 1 and reminded != 1:
            stage, text = 1, REMIND_1_DAY
        elif 1 < days_left <= 3 and reminded == 0:
            stage, text = 3, REMIND_3_DAYS
        if stage is None:
            continue

        try:
            await bot.send_message(int(user_id), text, reply_markup=tribute_keyboard())
        except Exception:
            continue
        c = None
        try:
            c = get_db()
            c.run("UPDATE members SET reminded_stage=:st WHERE user_id=:uid",
                  st=stage, uid=int(user_id))
        except Exception:
            pass
        finally:
            if c:
                c.close()


async def build_status_text(bot: Optional[Bot] = None) -> str:
    if not DATABASE_URL:
        return "База не подключена, статус недоступен."

    now = datetime.now()
    conn = None
    try:
        conn = get_db()
        active_month = conn.run(
            """SELECT username, end_dt, in_chat FROM members
               WHERE status='active' AND end_dt IS NOT NULL ORDER BY end_dt"""
        )
        forever_rows = conn.run(
            "SELECT in_chat FROM members WHERE status='active' AND end_dt IS NULL"
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
    in_group = sum(1 for _u, _e, ic in active_month if ic) + sum(1 for (ic,) in forever_rows if ic)

    group_total = None
    if bot:
        try:
            group_total = await bot.get_chat_member_count(CLUB_CHAT_ID)
        except Exception:
            pass

    lines = [
        "✅ <b>Бот на посту (режим уборки)</b>",
        "",
        f"👥 Участников Telegram: <b>{group_total if group_total is not None else '—'}</b>",
        f"💳 Активных записей в базе бота: <b>{active_total}</b>",
        f"✅ Из них в группе: <b>{in_group}</b>",
        f"💎 Навсегда: <b>{forever_n}</b>",
        f"🚪 Истёкших: <b>{expired_n}</b>",
    ]
    upcoming = [(u, e) for u, e, ic in active_month if ic]
    if upcoming:
        lines += ["", "<b>Ближайшие окончания:</b>"]
        for username, end_dt in upcoming[:10]:
            days_left = max(0, int((end_dt - now).total_seconds() / 86400))
            mark = "⚠️" if days_left <= 3 else "🔹"
            lines.append(
                f"{mark} @{username or '—'} — до {end_dt.strftime('%d.%m.%Y')} (осталось {days_left} дн.)"
            )
    return "\n".join(lines)


async def daily_report(bot: Bot):
    try:
        await sync_members(bot)
        today = datetime.now().strftime("%d.%m.%Y")
        text = await build_status_text(bot)
        await bot.send_message(ADMIN_ID, f"🌙 <b>Итог дня — {today}</b>\n\n{text}")
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отправить отчёт: %s", e)


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
            rows = conn.run("SELECT user_id, username FROM members WHERE user_id=:uid", uid=user_id)
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


def _add_to_blacklist(user_id: Optional[int], username: Optional[str]):
    if not DATABASE_URL or not user_id:
        return
    conn = None
    try:
        conn = get_db()
        uname = _clean_username(username or "") if username else None
        conn.run(
            """INSERT INTO blacklisted_users (user_id, username, added_at)
               VALUES (:uid, :un, :ts)
               ON CONFLICT (user_id) DO UPDATE SET username=:un""",
            uid=user_id, un=uname, ts=datetime.now(),
        )
    finally:
        if conn:
            conn.close()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Синхронизировать группу", callback_data="admin_sync")],
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="admin_status"),
                InlineKeyboardButton(text="👥 Участники", callback_data="admin_members"),
            ],
            [InlineKeyboardButton(text="🔁 Перевести на трибьют (убрать из бота)", callback_data="adm_ask:tribute")],
            [InlineKeyboardButton(text="✂️ Чистка ошибочных продлений", callback_data="adm_fixbug")],
            [
                InlineKeyboardButton(text="🔎 Найти", callback_data="adm_ask:find"),
                InlineKeyboardButton(text="➕ Добавить", callback_data="adm_ask:add"),
            ],
            [
                InlineKeyboardButton(text="📆 Продлить", callback_data="adm_ask:extend"),
                InlineKeyboardButton(text="🚪 Удалить", callback_data="adm_ask:remove"),
            ],
            [
                InlineKeyboardButton(text="⛔ Бан", callback_data="adm_ask:ban"),
                InlineKeyboardButton(text="🧹 Отозвать ссылки", callback_data="admin_revoke_invites"),
            ],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "⚙️ <b>Панель управления</b>\n\n"
        "Оплата через бота отключена. Бот только удаляет тех, у кого закончился срок.",
        reply_markup=admin_keyboard(),
    )


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


@router.callback_query(F.data == "admin_revoke_invites")
async def on_admin_revoke_invites(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    await cb.answer("Отзываю ссылки…")
    result = await revoke_all_bot_invites(bot)
    await cb.message.answer(
        "🧹 <b>Сброс ссылок завершён</b>\n\n"
        f"Найдено активных: <b>{result['found']}</b>\n"
        f"Отозвано: <b>{result['revoked']}</b>\n"
        f"Не удалось: <b>{result['failed']}</b>\n\n"
        "Людей эта кнопка не удаляет.",
        reply_markup=admin_keyboard(),
    )


@router.message(Command("revoke_invites"))
async def cmd_revoke_invites(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    await message.answer("🧹 Отзываю все сохранённые ссылки бота…")
    result = await revoke_all_bot_invites(bot)
    await message.answer(
        f"Найдено: <b>{result['found']}</b>, отозвано: <b>{result['revoked']}</b>, "
        f"ошибок: <b>{result['failed']}</b>"
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
            """SELECT user_id, username, tariff, start_dt, end_dt, status, in_chat, chat_status
               FROM members WHERE status='active' ORDER BY end_dt NULLS LAST, username"""
        )
    except Exception as e:  # noqa: BLE001
        return f"Ошибка базы: {e}"
    finally:
        if conn:
            conn.close()

    if not rows:
        return "В базе бота нет активных участников."

    lines = [f"👥 <b>Активных записей: {len(rows)}</b>", ""]
    for i, (uid, username, _t, _s, end_dt, _st, in_chat, _cs) in enumerate(rows, 1):
        if end_dt:
            days = max(0, int((end_dt - datetime.now()).total_seconds() / 86400))
            period = f"до {end_dt.strftime('%d.%m.%Y')} ({days} дн.)"
        else:
            period = "навсегда"
        presence = "✅ в группе" if in_chat else "⚠️ не в группе"
        lines.append(f"{i}. @{username or '—'} <code>{uid}</code> — {period} — {presence}")
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
            f"⚠️ <b>В базе записи нет</b>\nid: <code>{uid}</code>\n"
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
        f"статус: {record['status']}\n"
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

    uid, uname = _parse_target(parts[1])
    term = parts[2].lower()
    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer(f"Не знаю id для @{uname}. Укажи числовой id.")
        return

    start_dt = datetime.now()
    if term == "forever":
        end_dt, tariff = None, "forever"
    else:
        try:
            days = int(term)
        except ValueError:
            await message.answer("Укажи число дней или forever.")
            return
        if days <= 0:
            await message.answer("Количество дней должно быть больше нуля.")
            return
        end_dt, tariff = start_dt + timedelta(days=days), "month"

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
        await message.answer("Пользователя нет в базе.")
        return
    if record["end_dt"] is None:
        await message.answer("У пользователя доступ навсегда 💎. Продлевать нечего.")
        return

    new_end = max(datetime.now(), record["end_dt"]) + timedelta(days=int(parts[2]))
    conn = None
    try:
        conn = get_db()
        conn.run(
            "UPDATE members SET end_dt=:e, status='active', reminded_stage=0 WHERE user_id=:uid",
            uid=uid, e=new_end,
        )
        await message.answer(
            f"✅ @{uname or uid} продлён на {parts[2]} дн. Новый срок: <b>{new_end.strftime('%d.%m.%Y')}</b>"
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
            """UPDATE members SET status='expired', in_chat=FALSE, chat_status='removed_by_admin'
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
        await message.answer(f"✅ В базе доступ закрыт.\n⚠️ В Telegram удалить не удалось: {e}")


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
            """UPDATE members SET status='expired', in_chat=FALSE, chat_status='blacklisted'
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
        await message.answer(f"⛔ @{uname or uid} в чёрном списке.\n⚠️ Заблокировать в Telegram не удалось: {e}")


@router.message(Command("sync"))
async def cmd_sync(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    await message.answer("🔄 Запускаю сверку базы с Telegram-группой…")
    await message.answer(await sync_report_text(bot))


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "/admin — панель\n"
        "/status — статус\n"
        "/sync — сверить базу с группой\n"
        "/members — активные записи\n"
        "/find @username — найти человека\n"
        "/add @username 30 — добавить на 30 дней\n"
        "/extend @username 30 — продлить\n"
        "/remove @username — кикнуть из клуба\n"
        "/ban @username — чёрный список + бан\n"
        "/revoke_invites — отозвать ссылки, созданные ботом\n"
        "/fix_bug — разовая чистка ошибочно продлённых доступов\n"
        "/tribute @username — человек оформил Tribute, убрать из базы бота\n"
    )


# ---------------------------------------------------------------------------
# Разовая чистка: ошибочно продлённые доступы
# ---------------------------------------------------------------------------

BUG_GROUP_A = {
    6882319264: ("Zoki3333", "2026-10-07 09:23:00"),
    960370831: ("The_Olga_m", "2026-10-07 16:51:00"),
    712302633: ("karpri", "2026-10-11 07:54:00"),
    329228801: ("dmitry_port", "2026-10-16 18:36:00"),
    5243358806: ("Danny_Xx", "2026-10-08 01:10:00"),
}

BUG_GROUP_B = {
    608912079: ("stacie_dusk_archive", "2026-09-11 09:13:49"),
    1403697313: ("AlienMorozova", "2026-09-12 00:31:48"),
    1323822987: ("Alla_na_Vibe", "2026-09-12 08:49:41"),
    6050825317: ("norimyxxxomi", "2026-09-13 07:14:49"),
    872400982: ("Kuzina_Nataliaa", "2026-09-13 12:53:00"),
    5111672972: ("leonN227", "2026-09-16 12:43:13"),
    844586486: ("selena_visums", "2026-09-18 08:00:36"),
    5423347940: ("alexandrosid", "2026-09-19 21:17:20"),
    1161325653: ("Lenoshkavesna", "2026-09-22 20:45:10"),
    1812080881: ("katya_illa", "2026-09-22 18:16:28"),
    5567657328: ("tatviis", "2026-09-19 05:59:58"),
    1475596051: ("avekr", "2026-09-24 12:12:51"),
    425354894: ("chernykh_helen", "2026-09-25 13:16:15"),
    485449344: ("smusik", "2026-09-28 08:30:36"),
    1193140488: ("Leisan_Mos_i", "2026-09-30 19:18:10"),
    7517380516: ("NikiforovaVeraFoto", "2026-09-18 15:21:05"),
    267881574: ("albinamuravleva", "2026-09-20 21:19:24"),
    5463532917: ("yanapononarenko", "2026-09-23 08:29:13"),
    895791842: ("alla_astro_moon", "2026-09-27 02:09:33"),
}


def _bug_group(key: str) -> dict:
    return BUG_GROUP_A if key == "A" else BUG_GROUP_B


def _bug_preview_text() -> str:
    lines = ["🧾 <b>Ошибочно продлённые доступы</b>", ""]
    for key, title in (("A", "Группа A: доступ до ноября/декабря"), ("B", "Группа B: 60 дней вместо 30 (до конца октября)")):
        grp = _bug_group(key)
        lines.append(f"<b>{title}</b> ({len(grp)} чел.)")
        for uid, (uname, correct_end) in grp.items():
            rec = get_member_record(uid)
            now_end = rec["end_dt"].strftime("%d.%m") if rec and rec["end_dt"] else "—"
            fix = datetime.strptime(correct_end, "%Y-%m-%d %H:%M:%S")
            when = "сразу удалят" if fix <= datetime.now() else f"удалят {fix.strftime('%d.%m')}"
            lines.append(f"• @{uname}: сейчас до {now_end}, правильно до {fix.strftime('%d.%m')} ({when})")
        lines.append("")
    lines.append("Нажми кнопку нужной группы. Бот поставит правильную дату, и удаление пройдёт автоматически.")
    return "\n".join(lines)


def _bug_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✂️ Применить группе A ({len(BUG_GROUP_A)})", callback_data="fixbug:A")],
            [InlineKeyboardButton(text=f"✂️ Применить группе B ({len(BUG_GROUP_B)})", callback_data="fixbug:B")],
        ]
    )


@router.message(Command("fix_bug"))
async def cmd_fix_bug(message: Message):
    if not _is_admin(message):
        return
    await message.answer(_bug_preview_text(), reply_markup=_bug_keyboard())


@router.callback_query(F.data.startswith("fixbug:"))
async def on_fix_bug(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    if key not in ("A", "B"):
        await cb.answer("Неизвестная группа", show_alert=True)
        return
    await cb.answer("Применяю…")

    changed, skipped = [], []
    for uid, (uname, correct_end) in _bug_group(key).items():
        rec = get_member_record(uid)
        if not rec or rec["status"] != "active":
            skipped.append(f"@{uname} (уже не активен)")
            continue
        new_end = datetime.strptime(correct_end, "%Y-%m-%d %H:%M:%S")
        # Защита: никогда не удлиняем срок и не трогаем тех, у кого он уже короче.
        if rec["end_dt"] is not None and rec["end_dt"] <= new_end:
            skipped.append(f"@{uname} (срок уже {rec['end_dt'].strftime('%d.%m')}, не менял)")
            continue
        conn = None
        try:
            conn = get_db()
            conn.run(
                "UPDATE members SET end_dt=:e, reminded_stage=0 WHERE user_id=:uid",
                e=new_end, uid=uid,
            )
            changed.append(f"@{uname} → до {new_end.strftime('%d.%m')}")
        except Exception as e:  # noqa: BLE001
            skipped.append(f"@{uname} (ошибка базы: {e})")
        finally:
            if conn:
                conn.close()

    await cb.message.answer(
        f"✅ <b>Группа {key}: сроки исправлены</b>\n\n"
        f"Изменено: <b>{len(changed)}</b>\n" + "\n".join(changed[:40])
        + (f"\n\nПропущено: <b>{len(skipped)}</b>\n" + "\n".join(skipped[:40]) if skipped else "")
        + "\n\nТех, у кого срок уже прошёл, сейчас удаляю."
    )
    # Сразу запускаем удаление просроченных, не ждём часового цикла.
    await check_expired(bot)
    await cb.message.answer("🚪 Проверка просроченных выполнена. Подробности придут отдельными сообщениями.")


@router.message(Command("tribute"))
async def cmd_tribute(message: Message):
    """Убирает людей из базы бота: дальше ими управляет Tribute. Из клуба НЕ удаляет."""
    if not _is_admin(message):
        return
    tokens = (message.text or "").replace(",", " ").split()[1:]
    if not tokens:
        await message.answer(
            "Пример: <code>/tribute @username</code>\n"
            "Можно сразу несколько: <code>/tribute @ник1 @ник2 123456789</code>\n\n"
            "Бот забудет этих людей и больше не будет их удалять. Из клуба они не пропадут."
        )
        return

    removed, missing = [], []
    for tok in tokens:
        uid, uname = _parse_target(tok)
        if uid is None:
            uid, found = await _lookup_in_db(username=uname)
            if found:
                uname = found
        if uid is None:
            missing.append(f"@{uname}")
            continue
        rec = get_member_record(uid)
        if not rec:
            missing.append(f"@{uname or uid}")
            continue
        conn = None
        try:
            conn = get_db()
            conn.run("DELETE FROM members WHERE user_id=:uid", uid=uid)
            removed.append(f"@{rec['username'] or uid}")
        except Exception as e:  # noqa: BLE001
            missing.append(f"@{uname or uid} (ошибка базы: {e})")
        finally:
            if conn:
                conn.close()

    text = []
    if removed:
        text.append("✅ Теперь за них отвечает Tribute, из базы бота убраны:\n" + "\n".join(removed))
    if missing:
        text.append("ℹ️ Не нашла в базе бота (значит, бот и так их не трогает):\n" + "\n".join(missing))
    await message.answer("\n\n".join(text))


ASK_PROMPTS = {
    "tribute": "Напиши @ники людей, которые оформили подписку через Tribute (можно несколько через пробел). Бот перестанет их контролировать, из клуба они не пропадут.",
    "find": "Напиши @ник или id человека.",
    "add": "Напиши @ник (или id) и срок: <code>@ник 30</code> или <code>@ник forever</code>.",
    "extend": "Напиши @ник и сколько дней добавить: <code>@ник 30</code>.",
    "remove": "Напиши @ник, которого нужно удалить из клуба.",
    "ban": "Напиши @ник, которого нужно забанить и внести в чёрный список.",
}


@router.callback_query(F.data.startswith("adm_ask:"))
async def on_adm_ask(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    action = cb.data.split(":", 1)[1]
    if action not in ASK_PROMPTS:
        await cb.answer("Неизвестное действие", show_alert=True)
        return
    await state.update_data(action=action)
    await state.set_state(AdminAsk.waiting)
    await cb.message.answer(ASK_PROMPTS[action] + "\n\nОтмена: /cancel")
    await cb.answer()


@router.message(StateFilter(AdminAsk.waiting))
async def on_adm_answer(message: Message, state: FSMContext, bot: Bot):
    if not _is_admin(message):
        return
    text = (message.text or "").strip()
    if text.lower().startswith("/cancel"):
        await state.clear()
        await message.answer("Отменила.", reply_markup=admin_keyboard())
        return
    data = await state.get_data()
    action = data.get("action")
    await state.clear()

    fake = message.model_copy(update={"text": f"/{action} {text}"}).as_(bot)
    if action == "tribute":
        await cmd_tribute(fake)
    elif action == "find":
        await cmd_find(fake, bot)
    elif action == "add":
        await cmd_add(fake)
    elif action == "extend":
        await cmd_extend(fake)
    elif action == "remove":
        await cmd_remove(fake, bot)
    elif action == "ban":
        await cmd_ban(fake, bot)
    else:
        await message.answer("Не поняла действие, открой /admin заново.")


@router.callback_query(F.data == "adm_fixbug")
async def on_adm_fixbug(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(_bug_preview_text(), reply_markup=_bug_keyboard())


# ---------------------------------------------------------------------------
# Пользовательский путь: оплаты больше нет, только приветствие
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(greeting_text(), reply_markup=tribute_keyboard())


# Старые кнопки из прошлых сообщений (тарифы, оферта, продление) больше не ведут к оплате.
@router.callback_query(
    F.data.in_({"tariff_month", "tariff_forever", "renew_month", "accept_oferta", "accept_warning"})
)
async def on_old_buttons(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer(greeting_text(), reply_markup=tribute_keyboard())
    await cb.answer()


# Любое другое сообщение от обычного пользователя в личке (в том числе email из старого диалога).
@router.message(F.chat.type == "private", StateFilter(None))
async def on_any_private(message: Message):
    if _is_admin(message):
        return
    await message.answer(greeting_text(), reply_markup=tribute_keyboard())


# ---------------------------------------------------------------------------
# ЮMoney: оплата закрыта
# ---------------------------------------------------------------------------


def verify_yoomoney_sign(params: dict) -> bool:
    if not YOOMONEY_NOTIFICATION_SECRET:
        return False
    sign = (params.get("sign") or "").strip().lower()
    if not sign:
        return False
    parts = []
    for key in sorted(k for k in params.keys() if k != "sign"):
        value = "" if params.get(key) is None else str(params.get(key))
        parts.append(f"{key}={quote(value, safe='-_.~')}")
    calculated = hmac.new(
        YOOMONEY_NOTIFICATION_SECRET.encode("utf-8"),
        "&".join(parts).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().lower()
    return hmac.compare_digest(calculated, sign)


async def handle_yoomoney_pay(_request: web.Request) -> web.Response:
    # Старые платёжные ссылки больше не открывают форму оплаты.
    return web.Response(status=410, text="Оплата через этого бота закрыта. Подписка оформляется через Tribute.")


async def handle_yoomoney_notification(request: web.Request) -> web.Response:
    """Доступ НЕ выдаётся. Если вдруг пришёл настоящий платёж, сообщаем админу."""
    try:
        form = await request.post()
        params = {k: str(v) for k, v in form.items()}
    except Exception:
        return web.Response(status=400, text="BAD REQUEST")

    if not params or not verify_yoomoney_sign(params):
        return web.Response(status=403, text="INVALID SIGN")

    if str(params.get("test_notification", "")).lower() == "true":
        return web.Response(status=200, text="OK")

    try:
        await request.app["bot"].send_message(
            ADMIN_ID,
            "⚠️ <b>Пришёл платёж ЮMoney, хотя оплата в боте закрыта</b>\n\n"
            f"Метка: <code>{params.get('label', '—')}</code>\n"
            f"Сумма: {params.get('withdraw_amount') or params.get('amount') or '—'} ₽\n"
            f"Операция: <code>{params.get('operation_id', '—')}</code>\n\n"
            "Доступ автоматически НЕ выдан. Реши вручную: вернуть деньги или выдать доступ через /add.",
        )
    except Exception:
        pass
    return web.Response(status=200, text="OK")


async def handle_ping(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


async def on_startup(bot: Bot):
    try:
        await bot.set_my_commands(
            [BotCommand(command="start", description="Как попасть в клуб")],
            scope=BotCommandScopeDefault(),
        )
        await bot.set_my_commands(
            [
                BotCommand(command="admin", description="⚙️ Панель админа"),
                BotCommand(command="status", description="📊 Статус"),
                BotCommand(command="sync", description="🔄 Сверить группу"),
                BotCommand(command="members", description="👥 Участники в базе"),
                BotCommand(command="find", description="🔎 Найти"),
                BotCommand(command="add", description="➕ Добавить"),
                BotCommand(command="extend", description="➕ Продлить"),
                BotCommand(command="remove", description="🚪 Удалить"),
                BotCommand(command="ban", description="⛔ Заблокировать"),
                BotCommand(command="revoke_invites", description="🧹 Сбросить ссылки"),
                BotCommand(command="fix_bug", description="✂️ Чистка ошибочных продлений"),
                BotCommand(command="tribute", description="🔁 Перевести на трибьют"),
                BotCommand(command="help", description="🛠 Все команды"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
        me = await bot.get_me()
        log.info("Бот запущен: @%s (id=%s)", me.username, me.id)
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

    # При каждом запуске закрываем все старые инвайт-ссылки, которые создавал бот.
    try:
        res = await revoke_all_bot_invites(bot)
        log.info("Отзыв старых ссылок при старте: %s", res)
        if res["found"]:
            await bot.send_message(
                ADMIN_ID,
                f"🧹 При запуске отозваны старые ссылки бота: {res['revoked']} из {res['found']}"
                + (f" (не удалось: {res['failed']})" if res["failed"] else ""),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось отозвать старые ссылки при старте: %s", e)

    try:
        await background_sync(bot)
    except Exception as e:  # noqa: BLE001
        log.warning("Стартовый sync не выполнен: %s", e)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expired, "interval", hours=1, args=[bot])
    scheduler.add_job(check_reminders, "interval", hours=12, args=[bot])
    scheduler.add_job(background_sync, "interval", hours=6, args=[bot])
    scheduler.add_job(daily_report, CronTrigger(hour=20, minute=0), args=[bot])
    scheduler.start()

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/yoomoney/pay/{label}", handle_yoomoney_pay)
    for path in (
        "/yoomoney/notification",
        "/yoomoney/notification/",
        "/yoomoney/notification/{secret}",
        "/yoomoney/notification/{secret}/",
    ):
        app.router.add_get(path, handle_ping)
        app.router.add_post(path, handle_yoomoney_notification)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP-сервер слушает порт %s.", PORT)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
