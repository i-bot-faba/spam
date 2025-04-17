import inspect
from collections import namedtuple

# Создаём аналог устаревшего getargspec
ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")
def fix_getargspec(func):
    spec = inspect.getfullargspec(func)
    return ArgSpec(args=spec.args, varargs=spec.varargs, keywords=spec.varkw, defaults=spec.defaults)
inspect.getargspec = fix_getargspec

import pymorphy2
morph = pymorphy2.MorphAnalyzer()

import os
import asyncio
import re
import json
import nest_asyncio
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()
BANNED_FULL_NAMES = config.get("BANNED_FULL_NAMES", [])
PERMANENT_BLOCK_PHRASES = config.get("PERMANENT_BLOCK_PHRASES", [])
COMBINED_BLOCKS = config.get("COMBINED_BLOCKS", [])
BANNED_SYMBOLS = config.get("BANNED_SYMBOLS", [])
ADMIN_CHAT_ID = 296920330  # Твой ID

def get_tyumen_time():
    return (datetime.utcnow() + timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S')

def get_chat_link(chat):
    if chat.username:
        return f"https://t.me/{chat.username}"
    elif chat.title:
        return f"https://t.me/{chat.title.replace(' ', '')}"
    else:
        return f"Chat ID: {chat.id}"

def normalize_text(text: str) -> str:
    mapping = {'a': 'а','c': 'с','e': 'е','o': 'о','p': 'р','y': 'у','x': 'х','3': 'з','0': 'о'}
    return ''.join(mapping.get(ch, ch) for ch in text.lower())

def lemmatize_text(text: str) -> str:
    words = text.split()
    lemmatized = [morph.parse(word)[0].normal_form for word in words]
    return ' '.join(lemmatized)

async def send_admin_notification(bot, text: str):
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Ошибка отправки админу:", e)

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    text = msg.text
    processed_text = lemmatize_text(normalize_text(text))
    user = msg.from_user
    full_name = user.first_name or ""
    if user.last_name:
        full_name += f" | {user.last_name}"
    normalized_name = lemmatize_text(normalize_text(full_name))

    print("Имя:", full_name)
    print("Символы в имени:", list(full_name))
    print("Текст сообщения:", processed_text)

    permanent_ban = False

    if normalized_name in [lemmatize_text(normalize_text(n)) for n in BANNED_FULL_NAMES]:
        print("Совпадение по имени")
        permanent_ban = True

    if any(symbol in full_name for symbol in BANNED_SYMBOLS):
        print("Совпадение по символу в имени")
        permanent_ban = True

    for phrase in PERMANENT_BLOCK_PHRASES:
        if lemmatize_text(normalize_text(phrase)) in processed_text:
            print("Совпадение по фразе")
            permanent_ban = True
            break

    for combo in COMBINED_BLOCKS:
        if all(lemmatize_text(normalize_text(word)) in processed_text for word in combo):
            print("Совпадение по комбинации")
            permanent_ban = True
            break

    if permanent_ban:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
            await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
            print("Пользователь забанен:", full_name)
            notif = (
                f"Забанен: @{user.username or user.first_name}\n"
                f"Имя: {full_name}\n"
                f"Сообщение: {msg.text}"
            )
            await send_admin_notification(context.bot, notif)
        except Exception as e:
            print("Ошибка при блокировке:", e)

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не указан")
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    await app_bot.initialize()
    await app_bot.bot.set_webhook("https://your-app.onrender.com/webhook")  # Замени URL
    aio_app = web.Application()
    aio_app.router.add_get("/", lambda r: web.Response(text="OK"))
    aio_app.router.add_post("/webhook", lambda r: handle_webhook(r, app_bot))
    return aio_app, port

async def handle_webhook(request, app_bot):
    data = await request.json()
    update = Update.de_json(data, app_bot.bot)
    await app_bot.process_update(update)
    return web.Response(text="OK")

async def main():
    aio_app, port = await init_app()
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Server running on port {port}")
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
