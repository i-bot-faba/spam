import inspect
from collections import namedtuple
# === Fix для pymorphy2 ===
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
import time
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()

# === Загрузка конфига ===
def load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()
BANNED_FULL_NAMES       = config.get("BANNED_FULL_NAMES", [])
PERMANENT_BLOCK_PHRASES = config.get("PERMANENT_BLOCK_PHRASES", [])
COMBINED_BLOCKS         = config.get("COMBINED_BLOCKS", [])
BANNED_SYMBOLS          = config.get("BANNED_SYMBOLS", [])

ADMIN_CHAT_ID = 296920330

# === Помощники ===
def get_tyumen_time():
    return (datetime.utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")

def normalize_text(text: str) -> str:
    mapping = {'a':'а','c':'с','e':'е','o':'о','p':'р','y':'у','x':'х','3':'з','0':'о'}
    return "".join(mapping.get(ch, ch) for ch in text.lower())

def lemmatize_text(text: str) -> str:
    return " ".join(morph.parse(w)[0].normal_form for w in text.split())

async def send_admin_notification(bot, text: str):
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Ошибка отправки админу:", e)

# === Обработчик спама ===
async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    text = msg.text
    proc_text = lemmatize_text(normalize_text(text))
    user = msg.from_user
    # строим полное имя
    full_name = user.first_name or ""
    if user.last_name:
        full_name += " | " + user.last_name

    # Логируем для отладки
    print("▶️ Received from:", full_name)
    print("   Unicode chars:", [hex(ord(ch)) for ch in full_name])
    print("   BANNED_SYMBOLS:", BANNED_SYMBOLS)
    print("   Proc text:", proc_text)

    # нормализованное имя
    norm_name = lemmatize_text(normalize_text(full_name))
    banned_norm_names = [lemmatize_text(normalize_text(n)) for n in BANNED_FULL_NAMES]

    ban = False

    # по точному имени
    if norm_name in banned_norm_names:
        print("   ❌ Name match")
        ban = True

    # по символам в имени
    matched = [s for s in BANNED_SYMBOLS if s in full_name]
    if matched:
        print(f"   ❌ Symbol match: {matched}")
        ban = True

    # по фразам
    if not ban:
        for phrase in PERMANENT_BLOCK_PHRASES:
            if lemmatize_text(normalize_text(phrase)) in proc_text:
                print(f"   ❌ Phrase match: {phrase}")
                ban = True
                break

    # по комбинациям
    if not ban:
        for combo in COMBINED_BLOCKS:
            if all(lemmatize_text(normalize_text(w)) in proc_text for w in combo):
                print(f"   ❌ Combo match: {combo}")
                ban = True
                break

    if ban:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception as e:
            print("Ошибка удаления:", e)
        try:
            await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
            print("   ✅ User banned:", full_name)
        except Exception as e:
            print("Ошибка бана:", e)

        notif = (
            f"Забанен: @{user.username or user.first_name}\n"
            f"Имя: {full_name}\n"
            f"Дата: {get_tyumen_time()}\n"
            f"Сообщение: {text}"
        )
        await send_admin_notification(context.bot, notif)

# === Запуск ===
async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, delete_spam_message))

    await app.initialize()
    webhook_url = "https://spampython-bot-py.onrender.com"
    await app.bot.set_webhook(webhook_url)

    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="OK"))
    web_app.router.add_post("/webhook", lambda r: handle(r, app))
    return web_app, port

async def handle(request, app):
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(text="OK")

async def main():
    web_app, port = await init_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Server running on {port}")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
