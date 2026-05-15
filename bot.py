import os
import hashlib
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── НАСТРОЙКИ (заполни в Render → Environment Variables) ───────────────────
BOT_TOKEN        = os.environ.get('BOT_TOKEN')
CHANNEL_ID       = os.environ.get('CHANNEL_ID')       # напр. -1001234567890
RK_LOGIN         = os.environ.get('RK_LOGIN')          # логин в Робокассе
RK_PASSWORD1     = os.environ.get('RK_PASSWORD1')      # пароль #1
RK_PASSWORD2     = os.environ.get('RK_PASSWORD2')      # пароль #2
IS_TEST          = os.environ.get('IS_TEST', '1')      # '1' = тест, '0' = боевой
APP_URL          = os.environ.get('APP_URL', '')       # https://твой-бот.onrender.com
# ─────────────────────────────────────────────────────────────────────────────

TARIFFS = {
    1: {'name': '1 месяц',          'price': 690,  'days': 30},
    2: {'name': 'Навсегда 🔥 АКЦИЯ', 'price': 8990, 'days': None},
}

app = Flask(__name__)


# ══════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect('subs.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id   INTEGER PRIMARY KEY,
            username  TEXT,
            tariff_id INTEGER,
            start_dt  TEXT,
            end_dt    TEXT,
            status    TEXT DEFAULT 'active'
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            inv_id    INTEGER PRIMARY KEY,
            chat_id   INTEGER,
            tariff_id INTEGER,
            created   TEXT
        )
    ''')
    conn.commit()
    conn.close()


def db():
    return sqlite3.connect('subs.db')


# ══════════════════════════════════════════════════════
#  РОБОКАССА
# ══════════════════════════════════════════════════════

def make_inv_id(chat_id: int, tariff_id: int) -> int:
    """Уникальный номер счёта — сохраняем в БД."""
    ts = int(datetime.now().timestamp())
    inv_id = int(str(tariff_id) + str(abs(chat_id) % 100000) + str(ts % 10000))
    with db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO invoices VALUES (?,?,?,?)',
            (inv_id, chat_id, tariff_id, datetime.now().isoformat())
        )
    return inv_id


def robokassa_url(chat_id: int, tariff_id: int) -> str:
    tariff = TARIFFS[tariff_id]
    price  = tariff['price']
    inv_id = make_inv_id(chat_id, tariff_id)
    desc   = f"Adelin Creator Space — {tariff['name']}"

    # Shp-параметры ОБЯЗАТЕЛЬНО в алфавитном порядке
    shp = f"Shp_chat_id={chat_id}:Shp_tariff={tariff_id}"
    raw = f"{RK_LOGIN}:{price}:{inv_id}:{RK_PASSWORD1}:{shp}"
    sig = hashlib.md5(raw.encode()).hexdigest()

    test_param = "&IsTest=1" if IS_TEST == '1' else ""

    return (
        f"https://auth.robokassa.ru/Merchant/Index.aspx"
        f"?MerchantLogin={RK_LOGIN}"
        f"&OutSum={price}"
        f"&InvId={inv_id}"
        f"&Description={desc}"
        f"&SignatureValue={sig}"
        f"&Shp_chat_id={chat_id}"
        f"&Shp_tariff={tariff_id}"
        f"&Culture=ru"
        f"{test_param}"
    )


@app.route('/robokassa/result', methods=['POST'])
def robokassa_result():
    """Робокасса вызывает этот URL после успешной оплаты."""
    out_sum   = request.form.get('OutSum', '')
    inv_id    = request.form.get('InvId', '')
    signature = request.form.get('SignatureValue', '')
    chat_id   = request.form.get('Shp_chat_id', '')
    tariff_id = request.form.get('Shp_tariff', '')

    # Проверяем подпись
    shp = f"Shp_chat_id={chat_id}:Shp_tariff={tariff_id}"
    raw = f"{out_sum}:{inv_id}:{RK_PASSWORD2}:{shp}"
    expected = hashlib.md5(raw.encode()).hexdigest().upper()

    if signature.upper() != expected:
        logger.warning(f"Bad signature for inv_id={inv_id}")
        return 'bad sign', 400

    # Запускаем обработку в event loop бота
    asyncio.run_coroutine_threadsafe(
        process_payment(int(chat_id), int(tariff_id)),
        bot_loop
    )

    return f'OK{inv_id}'  # Робокасса ждёт именно такой ответ


@app.route('/health')
def health():
    return 'OK'


# ══════════════════════════════════════════════════════
#  ОБРАБОТКА ОПЛАТЫ
# ══════════════════════════════════════════════════════

async def process_payment(chat_id: int, tariff_id: int):
    tariff = TARIFFS[tariff_id]
    bot_instance = Bot(token=BOT_TOKEN)

    try:
        # Одноразовая ссылка в канал (действует 24 часа)
        invite = await bot_instance.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=datetime.now() + timedelta(hours=24),
            name=f"Оплата {chat_id}"
        )
        link = invite.invite_link
    except Exception as e:
        logger.error(f"Ошибка создания ссылки: {e}")
        link = "Не удалось создать ссылку, напиши @adelin_millerr"

    # Записываем подписку
    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=tariff['days']) if tariff['days'] else None

    with db() as conn:
        conn.execute(
            '''INSERT OR REPLACE INTO subscriptions 
               (chat_id, tariff_id, start_dt, end_dt, status)
               VALUES (?,?,?,?,?)''',
            (chat_id, tariff_id,
             start_dt.isoformat(),
             end_dt.isoformat() if end_dt else None,
             'active')
        )

    # Сообщение покупателю
    if end_dt:
        text = (
            f"🎉 Оплата прошла! Добро пожаловать в Adelin Creator Space\n\n"
            f"📅 Тариф: {tariff['name']}\n"
            f"⏳ Доступ до: {end_dt.strftime('%d.%m.%Y')}\n\n"
            f"👇 Твоя ссылка для входа в канал\n"
            f"(одноразовая, действует 24 часа):\n\n"
            f"{link}\n\n"
            f"За 3 дня до окончания я напомню о продлении 🤍"
        )
    else:
        text = (
            f"🎉 Оплата прошла! Добро пожаловать в Adelin Creator Space\n\n"
            f"♾️ Тариф: {tariff['name']}\n"
            f"✨ Доступ: навсегда\n\n"
            f"👇 Твоя ссылка для входа в канал\n"
            f"(одноразовая, действует 24 часа):\n\n"
            f"{link}"
        )

    await bot_instance.send_message(chat_id=chat_id, text=text)
    logger.info(f"Оплата обработана: chat_id={chat_id}, tariff={tariff_id}")


# ══════════════════════════════════════════════════════
#  TELEGRAM БОТ
# ══════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_chat.username or ''

    # Сохраняем username на будущее
    with db() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO subscriptions (chat_id, username, tariff_id, start_dt, status) VALUES (?,?,0,"","")',
            (chat_id, username)
        )
        conn.execute('UPDATE subscriptions SET username=? WHERE chat_id=?', (username, chat_id))

    url_month   = robokassa_url(chat_id, 1)
    url_forever = robokassa_url(chat_id, 2)

    keyboard = [
        [InlineKeyboardButton("💳 Оплатить — 1 месяц (690 ₽)", url=url_month)],
        [InlineKeyboardButton("🔥 Оплатить — Навсегда (8990 ₽) АКЦИЯ", url=url_forever)],
    ]

    text = (
        "Привет! Я помогу тебе получить доступ\n"
        "в закрытый канал Adelin Creator Space 🤍\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📅 *1 месяц* — 690 ₽\n"
        "   Полный доступ на 30 дней\n\n"
        "🔥 *Навсегда* — 8990 ₽\n"
        "   ~~19990 ₽~~ — специальная цена\n"
        "   Только до конца месяца!\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Выбирай тариф и нажимай кнопку 👇"
    )

    await update.message.reply_text(
        text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверить свою подписку."""
    chat_id = update.effective_chat.id

    with db() as conn:
        row = conn.execute(
            'SELECT tariff_id, end_dt, status FROM subscriptions WHERE chat_id=?',
            (chat_id,)
        ).fetchone()

    if not row or row[2] != 'active':
        await update.message.reply_text(
            "У тебя нет активной подписки.\n"
            "Оформи доступ через /start 🤍"
        )
        return

    tariff_id, end_dt, status = row
    tariff = TARIFFS.get(tariff_id, {})

    if end_dt:
        end = datetime.fromisoformat(end_dt).strftime('%d.%m.%Y')
        text = f"✅ Подписка активна\nТариф: {tariff.get('name','')}\nДействует до: {end}"
    else:
        text = f"✅ Подписка активна\nТариф: {tariff.get('name','')} — навсегда 🔥"

    await update.message.reply_text(text)


# ══════════════════════════════════════════════════════
#  ПЛАНИРОВЩИК (кик + напоминания)
# ══════════════════════════════════════════════════════

async def check_subscriptions():
    """Запускается каждый час."""
    bot_instance = Bot(token=BOT_TOKEN)
    now = datetime.now()

    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, end_dt FROM subscriptions WHERE end_dt IS NOT NULL AND status='active'"
        ).fetchall()

    for chat_id, end_dt_str in rows:
        end_dt = datetime.fromisoformat(end_dt_str)
        delta  = end_dt - now

        # Истекла
        if delta.total_seconds() <= 0:
            try:
                await bot_instance.ban_chat_member(chat_id=CHANNEL_ID, user_id=chat_id)
                await bot_instance.unban_chat_member(chat_id=CHANNEL_ID, user_id=chat_id)
                await bot_instance.send_message(
                    chat_id=chat_id,
                    text=(
                        "😔 Твоя подписка закончилась, и доступ к каналу закрыт.\n\n"
                        "Буду рада видеть тебя снова!\n"
                        "Продли доступ через /start 🤍"
                    )
                )
                with db() as conn:
                    conn.execute(
                        "UPDATE subscriptions SET status='expired' WHERE chat_id=?",
                        (chat_id,)
                    )
                logger.info(f"Кикнут пользователь {chat_id}")
            except Exception as e:
                logger.error(f"Ошибка кика {chat_id}: {e}")

        # Остаётся 3 дня — напоминаем
        elif 2 * 86400 < delta.total_seconds() <= 3 * 86400:
            try:
                end_str = end_dt.strftime('%d.%m.%Y')
                await bot_instance.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏰ Твоя подписка заканчивается {end_str}!\n\n"
                        f"Не теряй доступ к каналу — продли через /start 🤍"
                    )
                )
            except Exception as e:
                logger.error(f"Ошибка напоминания {chat_id}: {e}")


# ══════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════

bot_loop = None


def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


async def run_bot():
    global bot_loop
    bot_loop = asyncio.get_event_loop()

    init_db()

    # Планировщик
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_subscriptions, 'interval', hours=1)
    scheduler.start()

    # Бот
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler('start', cmd_start))
    application.add_handler(CommandHandler('status', cmd_status))

    logger.info("Бот запущен!")
    await application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    # Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Бот в основном потоке
    asyncio.run(run_bot())
