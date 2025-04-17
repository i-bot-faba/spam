import inspect
from collections import namedtuple

# --- Fix for pymorphy2 and Python 3.11+
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
import nest_asyncio
import time
import json
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
ADMIN_CHAT_ID = 296920330

def get_tyumen_time():
    return (datetime.utcnow() + timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S')

def get_chat_link(chat):
    if chat.username:
        return f"https://t.me/{chat.username}"
    elif chat.title:
        return f"https://t.me/{chat.title.replace(' ', '')}"
    return f"Chat ID: {chat.id}"

def normalize_text(text: str) -> str:
    mapping = {'a': 'а','c': 'с','e': 'е','o': 'о','p': 'р','y': 'у','x': 'х','3': 'з','0': 'о'}
    return ''.join(mapping.get(ch.lower(), ch.lower()) for ch in text)

def lemmatize_text(text: str) -> str:
    return ' '.join([morph.parse(word)[0].normal_form for word in text.split()])

async def send_admin_notification(bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Error sending admin notification:", e)

async def restrict_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg and msg.new_chat_members:
        for member in msg.new_chat_members:
            until_date = int(time.time()) + 300
            try:
                await context.bot.restrict_chat_member(
                    chat_id=msg.chat.id, user_id=member.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
            except Exception as e:
                print("Error restricting new member:", e)
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception as e:
            print("Error deleting join message:", e)

async def delete_left_member_notification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg and msg.left_chat_member:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception as e:
            print("Error deleting left message:", e)

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or update.channel_post
    if msg and msg.text:
        text = msg.text
        processed_text = lemmatize_text(normalize_text(text))
        permanent_ban = False
        user = msg.from_user

        full_name = user.first_name or ""
        if user.last_name:
            full_name += " | " + user.last_name

        normalized_name = lemmatize_text(normalize_text(full_name))
        banned_names = [lemmatize_text(normalize_text(n)) for n in BANNED_FULL_NAMES]

        if normalized_name in banned_names:
            permanent_ban = True

        if any(symbol in full_name for symbol in BANNED_SYMBOLS):
            permanent_ban = True

        for phrase in PERMANENT_BLOCK_PHRASES:
            if lemmatize_text(normalize_text(phrase)) in processed_text:
                permanent_ban = True
                break

        for combo in COMBINED_BLOCKS:
            if all(lemmatize_text(normalize_text(word)) in processed_text for word in combo):
                permanent_ban = True
                break

        if permanent_ban:
            try:
                await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                await send_admin_notification(context.bot,
                    f"Забанен: @{user.username or user.first_name}
Сообщение: {msg.text}")
            except Exception as e:
                print("Ban/delete error:", e)

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN not set")

    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, restrict_new_member))
    app_bot.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, delete_left_member_notification))
    app_bot.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    await app_bot.initialize()

    webhook_url = "https://your-app.onrender.com/webhook"
    await app_bot.bot.set_webhook(webhook_url)

    aio_app = web.Application()
    aio_app.router.add_get("/", lambda r: web.Response(text="OK"))
    aio_app.router.add_post("/webhook", lambda r: r.json().then(lambda d: app_bot.process_update(Update.de_json(d, app_bot.bot))).then(lambda: web.Response(text="OK")))
    return aio_app, port

async def main():
    aio_app, port = await init_app()
    runner = web.AppRunner(aio_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Bot running on port {port}")
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
