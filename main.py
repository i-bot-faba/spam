import inspect
from collections import namedtuple
import pymorphy2
import os
import asyncio
import re
import nest_asyncio
import time
import json
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")
inspect.getargspec = lambda func: ArgSpec(*inspect.getfullargspec(func)[:4])
morph = pymorphy2.MorphAnalyzer()
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

print("BANNED_SYMBOLS из config.json:", BANNED_SYMBOLS)

ADMIN_CHAT_ID = 296920330

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
    mapping = {
        'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о',
        'p': 'р', 'y': 'у', 'x': 'х', '3': 'з', '0': 'о'
    }
    return ''.join(mapping.get(ch, ch) for ch in text.lower())

def lemmatize_text(text: str) -> str:
    return ' '.join(morph.parse(word)[0].normal_form for word in text.split())

async def send_admin_notification(bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Error sending admin notification:", e)

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or update.channel_post
    if msg and msg.text:
        text = msg.text
        processed_text = lemmatize_text(normalize_text(text))
        print("🔍 Текст после нормализации и лемматизации:", processed_text)
        permanent_ban = False
        user = msg.from_user

        full_name = user.first_name if user.first_name else ""
        if user.last_name:
            full_name += " | " + user.last_name

        normalized_name = lemmatize_text(normalize_text(full_name))
        banned_names = [lemmatize_text(normalize_text(n)) for n in BANNED_FULL_NAMES]

        print("🔍 Проверка имени:", full_name)
        print("➡️ Unicode символы имени:", [ord(c) for c in full_name])
        print("➡️ Запрещённые символы:", BANNED_SYMBOLS)

        if normalized_name in banned_names:
            print(f"🚫 Совпадение с запрещённым именем: {full_name}")
            permanent_ban = True

        if any(symbol in full_name for symbol in BANNED_SYMBOLS):
            print(f"🚫 Найден запрещённый символ в имени: {full_name}")
            permanent_ban = True

        if not permanent_ban:
            for phrase in PERMANENT_BLOCK_PHRASES:
                if lemmatize_text(normalize_text(phrase)) in processed_text:
                    permanent_ban = True
                    break

        if not permanent_ban:
            for combo in COMBINED_BLOCKS:
                if all(lemmatize_text(normalize_text(w)) in processed_text for w in combo):
                    permanent_ban = True
                    break

        if permanent_ban:
            chat_link = get_chat_link(msg.chat)
            block_time = get_tyumen_time()
            username = f"@{user.username}" if user.username else user.first_name
            notif = (
                f"Забанен: {username}\n"
                f"Дата: {block_time}\n"
                f"Канал: {chat_link}\n"
                f"Сообщение: {msg.text}"
            )
            try:
                await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
                print("🗑 Сообщение удалено")
            except Exception as e:
                print("Ошибка удаления сообщения:", e)
            try:
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                print("🔨 Пользователь забанен")
            except Exception as e:
                print("Ошибка бана:", e)
            await send_admin_notification(context.bot, notif)

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN not set")
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    await app_bot.initialize()
    await app_bot.bot.set_webhook("https://your-app.onrender.com/webhook")
    aio_app = web.Application()
    aio_app.router.add_get("/", lambda r: web.Response(text="OK"))
    aio_app.router.add_post("/webhook", lambda r: r.json().then(lambda d: app_bot.process_update(Update.de_json(d, app_bot.bot)) or web.Response(text="OK")))
    return aio_app, port

async def main():
    aio_app, port = await init_app()
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"⚙️ Сервер запущен на порту {port}")
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
