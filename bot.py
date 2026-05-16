import os
import hashlib
import logging
import asyncio
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.environ.get('BOT_TOKEN')
CHANNEL_ID   = os.environ.get('CHANNEL_ID')
RK_LOGIN     = os.environ.get('RK_LOGIN')
RK_PASSWORD1 = os.environ.get('RK_PASSWORD1')
RK_PASSWORD2 = os.environ.get('RK_PASSWORD2')
IS_TEST      = os.environ.get('IS_TEST', '1')
DATABASE_URL = os.environ.get('DATABASE_URL')
OWNER_ID     = 1619432734

TARIFFS = {
    1: {'name': '1 месяц',        'price': 690,  'days': 30},
    2: {'name': 'Навсегда АКЦИЯ', 'price': 8990, 'days': None},
}

app = Flask(__name__)
main_loop = None


def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        chat_id BIGINT PRIMARY KEY, username TEXT,
        tariff_id INTEGER, start_dt TIMESTAMP, end_dt TIMESTAMP,
        status TEXT DEFAULT 'active'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS invoices (
        inv_id BIGINT PRIMARY KEY, chat_id BIGINT,
        tariff_id INTEGER, created TIMESTAMP
    )''')
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована!")


def make_inv_id(chat_id, tariff_id):
    ts = int(datetime.now().timestamp())
    inv_id = int(str(tariff_id) + str(abs(chat_id) % 100000) + str(ts % 10000))
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO invoices VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',
              (inv_id, chat_id, tariff_id, datetime.now()))
    conn.commit()
    conn.close()
    return inv_id


def robokassa_url(chat_id, tariff_id):
    t = TARIFFS[tariff_id]
    inv_id = make_inv_id(chat_id, tariff_id)
    shp = f"Shp_chat_id={chat_id}:Shp_tariff={tariff_id}"
    sig = hashlib.md5(f"{RK_LOGIN}:{t['price']}:{inv_id}:{RK_PASSWORD1}:{shp}".encode()).hexdigest()
    test = "&IsTest=1" if IS_TEST == '1' else ""
    return (f"https://auth.robokassa.ru/Merchant/Index.aspx"
            f"?MerchantLogin={RK_LOGIN}&OutSum={t['price']}&InvId={inv_id}"
            f"&Description=Creator+Lab&SignatureValue={sig}"
            f"&Shp_chat_id={chat_id}&Shp_tariff={tariff_id}&Culture=ru{test}")


@app.route('/robokassa/result', methods=['POST'])
def robokassa_result():
    out_sum = request.form.get('OutSum', '')
    inv_id  = request.form.get('InvId', '')
    sig     = request.form.get('SignatureValue', '')
    chat_id = request.form.get('Shp_chat_id', '')
    tariff  = request.form.get('Shp_tariff', '')
    shp     = f"Shp_chat_id={chat_id}:Shp_tariff={tariff}"
    expected = hashlib.md5(f"{out_sum}:{inv_id}:{RK_PASSWORD2}:{shp}".encode()).hexdigest().upper()
    if sig.upper() != expected:
        return 'bad sign', 400
    future = asyncio.run_coroutine_threadsafe(
        process_payment(int(chat_id), int(tariff)), main_loop)
    try:
        future.result(timeout=15)
    except Exception as e:
        logger.error(f"Payment error: {e}")
    return f'OK{inv_id}'


@app.route('/health')
def health():
    return 'OK'


async def process_payment(chat_id, tariff_id):
    t = TARIFFS[tariff_id]
    b = Bot(token=BOT_TOKEN)

    try:
        member = await b.get_chat(chat_id)
        username = f"@{member.username}" if member.username else f"id{chat_id}"
    except:
        username = f"id{chat_id}"

    try:
        invite = await b.create_chat_invite_link(
            chat_id=int(CHANNEL_ID), member_limit=1,
            expire_date=datetime.now() + timedelta(hours=24))
        link = invite.invite_link
    except Exception as e:
        logger.error(f"Invite error: {e}")
        link = "Напиши @adelin_millerr — вышлю ссылку вручную"

    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=t['days']) if t['days'] else None

    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO subscriptions (chat_id, tariff_id, start_dt, end_dt, status)
                 VALUES (%s,%s,%s,%s,'active')
                 ON CONFLICT (chat_id) DO UPDATE SET
                 tariff_id=%s, start_dt=%s, end_dt=%s, status='active' ''',
              (chat_id, tariff_id, start_dt, end_dt,
               tariff_id, start_dt, end_dt))
    conn.commit()
    conn.close()

    if end_dt:
        text = (f"Оплата прошла! Добро пожаловать в Creator Lab 🔐\n\n"
                f"Тариф: {t['name']}\n"
                f"Доступ до: {end_dt.strftime('%d.%m.%Y')}\n\n"
                f"Ссылка для входа в канал (одноразовая, действует 24 часа):\n\n{link}\n\n"
                f"За 3 дня до окончания напомню о продлении 🤍")
    else:
        text = (f"Оплата прошла! Добро пожаловать в Creator Lab 🔐\n\n"
                f"Тариф: {t['name']}\n"
                f"Доступ: навсегда\n\n"
                f"Ссылка для входа в канал (одноразовая, действует 24 часа):\n\n{link}")

    await b.send_message(chat_id=chat_id, text=text)

    try:
        end_str = end_dt.strftime('%d.%m.%Y') if end_dt else "навсегда"
        await b.send_message(
            chat_id=OWNER_ID,
            text=(f"Новая оплата! 🎉\n\n"
                  f"Тариф: {t['name']} — {t['price']} руб\n"
                  f"Покупатель: {username}\n"
                  f"chat_id: {chat_id}\n"
                  f"Доступ до: {end_str}")
        )
    except Exception as e:
        logger.error(f"Owner notify error: {e}")

    logger.info(f"Оплата: {chat_id}, тариф {tariff_id}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_chat.username or ''

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO subscriptions (chat_id, username, tariff_id, start_dt, status)
                     VALUES (%s,%s,0,NOW(),'')
                     ON CONFLICT (chat_id) DO UPDATE SET username=%s''',
                  (chat_id, username, username))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB error: {e}")

    try:
        url1 = robokassa_url(chat_id, 1)
        url2 = robokassa_url(chat_id, 2)
    except Exception as e:
        logger.error(f"URL error: {e}")
        await update.message.reply_text("Привет! Что-то пошло не так, попробуй ещё раз.")
        return

    keyboard = [
        [InlineKeyboardButton("💜 1 месяц — 690 руб (АКЦИЯ)", url=url1)],
        [InlineKeyboardButton("💜 Навсегда — 8990 руб (ВЫГОДНО)", url=url2)],
    ]

    text = (
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
        "Выбирай тариф 👇"
    )

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute('SELECT tariff_id, end_dt, status FROM subscriptions WHERE chat_id=%s', (chat_id,))
    row = c.fetchone()
    conn.close()

    if not row or row['status'] != 'active' or row['tariff_id'] == 0:
        await update.message.reply_text("У тебя нет активной подписки. Оформи через /start")
        return

    t = TARIFFS.get(row['tariff_id'], {})
    if row['end_dt']:
        end = row['end_dt'].strftime('%d.%m.%Y')
        await update.message.reply_text(f"Подписка активна\nТариф: {t.get('name','')}\nДо: {end}")
    else:
        await update.message.reply_text(f"Подписка активна\nТариф: {t.get('name','')} — навсегда!")


def check_subscriptions_sync():
    if main_loop:
        asyncio.run_coroutine_threadsafe(check_subscriptions(), main_loop)


async def check_subscriptions():
    b = Bot(token=BOT_TOKEN)
    now = datetime.now()
    conn = get_db()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT chat_id, end_dt FROM subscriptions WHERE end_dt IS NOT NULL AND status='active'")
    rows = c.fetchall()
    conn.close()

    for row in rows:
        chat_id = row['chat_id']
        end_dt = row['end_dt']
        delta = end_dt - now

        if delta.total_seconds() <= 0:
            try:
                await b.ban_chat_member(chat_id=int(CHANNEL_ID), user_id=chat_id)
                await b.unban_chat_member(chat_id=int(CHANNEL_ID), user_id=chat_id)
                await b.send_message(chat_id=chat_id,
                    text="Подписка закончилась, доступ закрыт. Приходи снова — /start 🤍")
                c2 = get_db()
                cur = c2.cursor()
                cur.execute("UPDATE subscriptions SET status='expired' WHERE chat_id=%s", (chat_id,))
                c2.commit()
                c2.close()
            except Exception as e:
                logger.error(f"Kick error {chat_id}: {e}")

        elif 2 * 86400 < delta.total_seconds() <= 3 * 86400:
            try:
                await b.send_message(chat_id=chat_id,
                    text=f"Подписка заканчивается {end_dt.strftime('%d.%m.%Y')}! Продли через /start 🤍")
            except Exception as e:
                logger.error(f"Reminder error {chat_id}: {e}")


def run_bot_in_thread():
    global main_loop
    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    async def start_bot():
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler('start', cmd_start))
        application.add_handler(CommandHandler('status', cmd_status))
        logger.info("Бот запущен!")
        async with application:
            await application.start()
            await application.updater.start_polling(drop_pending_updates=True)
            while True:
                await asyncio.sleep(3600)

    main_loop.run_until_complete(start_bot())


if __name__ == '__main__':
    init_db()

    bot_thread = threading.Thread(target=run_bot_in_thread, daemon=True)
    bot_thread.start()

    time.sleep(3)

    scheduler = BackgroundScheduler()
    scheduler.add_job(check_subscriptions_sync, 'interval', hours=1)
    scheduler.start()

    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Flask на порту {port}")
    app.run(host='0.0.0.0', port=port)
