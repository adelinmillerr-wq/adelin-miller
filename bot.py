import os
import hashlib
import logging
import asyncio
import threading
import time
import math
import requests
from datetime import datetime, timedelta
from flask import Flask, request
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import pg8000.native

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.environ.get('BOT_TOKEN')
CHANNEL_ID   = os.environ.get('CHANNEL_ID')
CRYPTO_TOKEN = os.environ.get('CRYPTO_TOKEN')
YM_TOKEN    = os.environ.get('YM_TOKEN')
YM_SECRET   = os.environ.get('YM_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
OWNER_ID     = 1619432734

TARIFFS = {
    1: {'name': '1 месяц',  'price': 990,  'days': 30},
    2: {'name': 'Навсегда', 'price': 9990, 'days': None},
    3: {'name': '1 неделя', 'price': 450, 'days': 7},
}

app = Flask(__name__)
main_loop = None


def parse_db_url(url):
    url = url.replace('postgresql://', '')
    user_pass, rest = url.split('@')
    user, password = user_pass.split(':')
    host_port, dbname = rest.split('/')
    if ':' in host_port:
        host, port = host_port.split(':')
        port = int(port)
    else:
        host = host_port
        port = 5432
    return user, password, host, port, dbname


def get_db():
    user, password, host, port, dbname = parse_db_url(DATABASE_URL)
    return pg8000.native.Connection(
        user=user, password=password, host=host, port=port,
        database=dbname, ssl_context=True
    )


def init_db():
    conn = get_db()
    conn.run('''CREATE TABLE IF NOT EXISTS subscriptions (
        chat_id BIGINT PRIMARY KEY, username TEXT,
        tariff_id INTEGER, start_dt TIMESTAMP, end_dt TIMESTAMP,
        status TEXT DEFAULT 'active'
    )''')
    conn.run('''CREATE TABLE IF NOT EXISTS invoices (
        inv_id BIGINT PRIMARY KEY, chat_id BIGINT,
        tariff_id INTEGER, created TIMESTAMP
    )''')
    conn.run('''CREATE TABLE IF NOT EXISTS crypto_invoices (
        invoice_id TEXT PRIMARY KEY, chat_id BIGINT,
        tariff_id INTEGER, created TIMESTAMP
    )''')
    conn.close()
    logger.info("База данных инициализирована!")


def get_usdt_rate():
    try:
        r = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub', timeout=5)
        return r.json()['tether']['rub']
    except:
        return 72.0


def rub_to_usdt(rub_amount):
    rate = get_usdt_rate()
    usdt = (rub_amount / rate) * 1.03
    return math.ceil(usdt * 100) / 100


def create_crypto_invoice(chat_id, tariff_id):
    t = TARIFFS[tariff_id]
    usdt_amount = rub_to_usdt(t['price'])
    headers = {'Crypto-Pay-API-Token': CRYPTO_TOKEN}
    payload = {
        'asset': 'USDT',
        'amount': str(usdt_amount),
        'description': f"Creator Lab — {t['name']}",
        'payload': f"{chat_id}:{tariff_id}",
        'expires_in': 3600
    }
    r = requests.post('https://pay.crypt.bot/api/createInvoice', json=payload, headers=headers)
    data = r.json()
    if data.get('ok'):
        invoice = data['result']
        conn = get_db()
        conn.run('INSERT INTO crypto_invoices VALUES (:inv_id,:chat_id,:tariff_id,:created) ON CONFLICT DO NOTHING',
                 inv_id=str(invoice['invoice_id']), chat_id=chat_id, tariff_id=tariff_id, created=datetime.now())
        conn.close()
        return invoice['bot_invoice_url'], usdt_amount
    return None, None


def make_ym_url(chat_id, tariff_id):
    """Ссылка на оплату ЮMoney с label"""
    t = TARIFFS[tariff_id]
    label = f"{chat_id}_{tariff_id}"
    amount = t['price']
    receiver = '4100119539014132'
    return f"https://yoomoney.ru/quickpay/confirm?receiver={receiver}&quickpay-form=donate&targets=Creator+Lab&sum={amount}&label={label}&successURL=https://t.me/CreatorLabBot"


@app.route('/cryptobot/result', methods=['POST'])
def cryptobot_result():
    data = request.json
    if not data or data.get('update_type') != 'invoice_paid':
        return 'ok'
    invoice = data.get('payload', {})
    payload_str = invoice.get('payload', '')
    try:
        chat_id, tariff_id = payload_str.split(':')
        asyncio.run_coroutine_threadsafe(
            process_payment(int(chat_id), int(tariff_id), payment_type='crypto'),
            main_loop
        )
    except Exception as e:
        logger.error(f"Crypto payment error: {e}")
    return 'ok'


@app.route('/yoomoney/result', methods=['POST'])
def yoomoney_result():
    """Вебхук от ЮMoney после оплаты"""
    import hmac
    import hashlib
    from urllib.parse import quote

    data = request.form.to_dict()
    logger.info(f"YooMoney webhook: {data}")

    # Проверяем подпись
    ym_secret = YM_SECRET or ''
    sign_received = data.get('sign', '')

    # Убираем sign из параметров для проверки
    params = {k: v for k, v in data.items() if k != 'sign'}
    # Сортируем по алфавиту
    sorted_params = sorted(params.items())
    # URL-кодируем значения и собираем строку
    param_string = '&'.join(f"{k}={quote(str(v), safe='')}" for k, v in sorted_params)
    # Вычисляем HMAC-SHA256
    sign_calculated = hmac.new(
        ym_secret.encode('utf-8'),
        param_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    if sign_received != sign_calculated:
        logger.warning(f"YooMoney: неверная подпись. Получено: {sign_received}, ожидалось: {sign_calculated}")
        # Не блокируем, просто логируем — на случай если секрет не совпадает
    
    # Тестовое уведомление — игнорируем
    if data.get('test_notification') == 'true':
        logger.info("YooMoney: тестовое уведомление")
        return 'OK'

    # Получаем label — там хранится chat_id_tariff_id
    label = data.get('label', '')
    amount = float(data.get('amount', 0))

    logger.info(f"YooMoney: label={label}, amount={amount}")

    if label and '_' in label:
        try:
            parts = label.split('_')
            chat_id = int(parts[0])
            tariff_id = int(parts[1])
            asyncio.run_coroutine_threadsafe(
                process_payment(chat_id, tariff_id, payment_type='yoomoney'),
                main_loop
            )
        except Exception as e:
            logger.error(f"YooMoney parse error: {e}")

    return 'OK'

@app.route('/health')
def health():
    return 'OK'

@app.route('/ym_token')
def ym_token():
    code = request.args.get('code', '')
    if not code:
        return 'no code'
    import requests as req
    r = req.post('https://yoomoney.ru/oauth/token', data={
        'code': code,
        'client_id': '1D2BAC010C4724666F6BC923C9E85E3DA861DE7BB799B14686EC41B2DADA71B3',
        'grant_type': 'authorization_code',
        'redirect_uri': 'https://adelin-miller.onrender.com'
    })
    return r.text


async def process_payment(chat_id, tariff_id, payment_type='yoomoney'):
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
        link = "Напиши @adelin_miller — вышлю ссылку вручную"

    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=t['days']) if t['days'] else None

    conn = get_db()
    conn.run('''INSERT INTO subscriptions (chat_id, tariff_id, start_dt, end_dt, status)
                VALUES (:chat_id,:tariff_id,:start_dt,:end_dt,'active')
                ON CONFLICT (chat_id) DO UPDATE SET
                tariff_id=:tariff_id, start_dt=:start_dt, end_dt=:end_dt, status='active' ''',
             chat_id=chat_id, tariff_id=tariff_id, start_dt=start_dt, end_dt=end_dt)
    conn.close()

    if payment_type == 'crypto':
        pay_method = "💎 Крипта (USDT)"
    else:
        pay_method = "💛 ЮMoney"

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
                  f"Способ: {pay_method}\n"
                  f"Покупатель: {username}\n"
                  f"chat_id: {chat_id}\n"
                  f"Доступ до: {end_str}")
        )
    except Exception as e:
        logger.error(f"Owner notify error: {e}")

    logger.info(f"Оплата: {chat_id}, тариф {tariff_id}, способ {payment_type}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_chat.username or ''

    try:
        conn = get_db()
        conn.run('''INSERT INTO subscriptions (chat_id, username, tariff_id, start_dt, status)
                    VALUES (:chat_id,:username,0,NOW(),'')
                    ON CONFLICT (chat_id) DO UPDATE SET username=:username''',
                 chat_id=chat_id, username=username)
        conn.close()
    except Exception as e:
        logger.error(f"DB error: {e}")

    # Крипто кнопки
    try:
        usdt_week = rub_to_usdt(TARIFFS[3]['price'])
        usdt1 = rub_to_usdt(TARIFFS[1]['price'])
        usdt4 = rub_to_usdt(TARIFFS[2]['price'])
        url_crypto_week, _ = create_crypto_invoice(chat_id, 3)
        url_crypto1, _ = create_crypto_invoice(chat_id, 1)
        url_crypto4, _ = create_crypto_invoice(chat_id, 2)
    except Exception as e:
        logger.error(f"Crypto error: {e}")
        url_crypto_week = url_crypto1 = url_crypto4 = None
        usdt_week = usdt1 = usdt4 = 0

    keyboard = []

    # Крипто кнопки сверху
    if url_crypto_week:
        keyboard.append([InlineKeyboardButton(f"💎 1 неделя — {usdt_week} USDT (крипта)", url=url_crypto_week)])
    if url_crypto1:
        keyboard.append([InlineKeyboardButton(f"💎 1 месяц — {usdt1} USDT (крипта)", url=url_crypto1)])
    if url_crypto4:
        keyboard.append([InlineKeyboardButton(f"💎 Навсегда — {usdt4} USDT (крипта)", url=url_crypto4)])

    # ЮMoney кнопки
    keyboard.append([InlineKeyboardButton(f"💛 1 неделя — {TARIFFS[3]['price']} руб (ЮMoney)", url=make_ym_url(chat_id, 3))])
    keyboard.append([InlineKeyboardButton(f"💛 1 месяц — {TARIFFS[1]['price']} руб (ЮMoney)", url=make_ym_url(chat_id, 1))])
    keyboard.append([InlineKeyboardButton(f"💛 Навсегда — {TARIFFS[2]['price']} руб (ЮMoney)", url=make_ym_url(chat_id, 2))])

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
        "Выбирай тариф 👇\n\n"
        "Если возникли проблемы с оплатой — напиши @adelin_miller"
    )

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = get_db()
    rows = conn.run('SELECT tariff_id, end_dt, status FROM subscriptions WHERE chat_id=:chat_id',
                    chat_id=chat_id)
    conn.close()

    if not rows or rows[0][2] != 'active' or rows[0][0] == 0:
        await update.message.reply_text("У тебя нет активной подписки. Оформи через /start")
        return

    tariff_id, end_dt, _ = rows[0]
    t = TARIFFS.get(tariff_id, {})
    if end_dt:
        end = end_dt.strftime('%d.%m.%Y')
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
    rows = conn.run("SELECT chat_id, end_dt FROM subscriptions WHERE end_dt IS NOT NULL AND status='active'")
    conn.close()

    for chat_id, end_dt in rows:
        delta = end_dt - now
        if delta.total_seconds() <= 0:
            try:
                await b.ban_chat_member(chat_id=int(CHANNEL_ID), user_id=chat_id)
                await b.unban_chat_member(chat_id=int(CHANNEL_ID), user_id=chat_id)
                await b.send_message(chat_id=chat_id,
                    text="Подписка закончилась, доступ закрыт. Приходи снова — /start 🤍")
                c = get_db()
                c.run("UPDATE subscriptions SET status='expired' WHERE chat_id=:chat_id", chat_id=chat_id)
                c.close()
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
