import inspect
from collections import namedtuple
import os
import asyncio
import re
import json
import nest_asyncio
import time
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)
import pymorphy2

# Fix для pymorphy2 на Python 3.11+
ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")
def fix_getargspec(func):
    spec = inspect.getfullargspec(func)
    return ArgSpec(args=spec.args, varargs=spec.varargs, keywords=spec.varkw, defaults=spec.defaults)
inspect.getargspec = fix_getargspec

morph = pymorphy2.MorphAnalyzer()
nest_asyncio.apply()

# --- Конфиг ---
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()
BANNED_FULL_NAMES         = config.get("BANNED_FULL_NAMES", [])
PERMANENT_BLOCK_PHRASES   = config.get("PERMANENT_BLOCK_PHRASES", [])
COMBINED_BLOCKS           = config.get("COMBINED_BLOCKS", [])
BANNED_SYMBOLS            = config.get("BANNED_SYMBOLS", [])
BANNED_NAME_SUBSTRINGS    = config.get("BANNED_NAME_SUBSTRINGS", [])
BANNED_WORDS              = config.get("BANNED_WORDS", [])

ADMIN_CHAT_ID = 296920330

def get_tyumen_time():
    return (datetime.utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")

def normalize_text(text: str) -> str:
    mapping = {
        'a':'а','c':'с','e':'е','o':'о','p':'р','y':'у','x':'х',
        '3':'з','0':'о'
    }
    return "".join(mapping.get(ch, ch) for ch in text.lower())

def lemmatize_text(text: str) -> str:
    return " ".join(morph.parse(w)[0].normal_form for w in text.split())

async def send_admin_notification(bot, text: str):
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Ошибка отправки админу:", e)

# --- Обработчик СПАМА ---
async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    text      = msg.text
    proc_text = lemmatize_text(normalize_text(text))
    user      = msg.from_user
    full_name = user.first_name or ""
    if user.last_name:
        full_name += " | " + user.last_name

    clean_name = re.sub(r'[\uFE00-\uFE0F\u200D]', '', full_name)
    name_lower = normalize_text(clean_name)

    print("▶️ From:", full_name, "| Clean:", clean_name)

    ban = False
    # 💋 в имени
    if "💋" in clean_name:
        print("   ❌ Found 💋 in name, banning immediately")
        ban = True

    # Подстроки в имени
    if not ban:
        for substr in BANNED_NAME_SUBSTRINGS:
            if normalize_text(substr) in name_lower:
                print(f"   ❌ Substring match in name: {substr}")
                ban = True
                break

    # Точное имя
    if not ban:
        norm_name    = lemmatize_text(name_lower)
        banned_norms = [lemmatize_text(normalize_text(n)) for n in BANNED_FULL_NAMES]
        if norm_name in banned_norms:
            print("   ❌ Full name match")
            ban = True

    # Символы в имени
    if not ban:
        matched = [s for s in BANNED_SYMBOLS if s in clean_name]
        if matched:
            print(f"   ❌ Symbol match: {matched}")
            ban = True

    # Фразы в сообщении
    if not ban:
        for phrase in PERMANENT_BLOCK_PHRASES:
            if lemmatize_text(normalize_text(phrase)) in proc_text:
                print(f"   ❌ Phrase match: {phrase}")
                ban = True
                break

    # Комбинации
    if not ban:
        for combo in COMBINED_BLOCKS:
            if all(lemmatize_text(normalize_text(w)) in proc_text for w in combo):
                print(f"   ❌ Combo match: {combo}")
                ban = True
                break

    # Слова из BANNED_WORDS
    if not ban:
        for w in BANNED_WORDS:
            if w.lower() in text.lower():
                print(f"   ❌ BANNED_WORD found: {w}")
                ban = True
                break

    if ban:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception as e:
            print("Ошибка удаления:", e)
        try:
            await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
            print("   ✅ Banned user:", clean_name)
        except Exception as e:
            print("Ошибка бана:", e)
        notif = (
            f"Забанен: @{user.username or user.first_name}\n"
            f"Имя: {clean_name}\n"
            f"Дата: {get_tyumen_time()}\n"
            f"Сообщение: {text}"
        )
        await send_admin_notification(context.bot, notif)

# --- КОМАНДЫ ДЛЯ СПИСКА И ДОБАВЛЕНИЯ СПАМА ---

async def spamlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет доступа.")
        return
    parts = []
    if BANNED_FULL_NAMES:
        parts.append("BANNED_FULL_NAMES:\n" + "\n".join(BANNED_FULL_NAMES))
    if PERMANENT_BLOCK_PHRASES:
        parts.append("PERMANENT_BLOCK_PHRASES:\n" + "\n".join(PERMANENT_BLOCK_PHRASES))
    if COMBINED_BLOCKS:
        cblocks = ["; ".join(combo) for combo in COMBINED_BLOCKS]
        parts.append("COMBINED_BLOCKS:\n" + "\n".join(cblocks))
    if BANNED_SYMBOLS:
        parts.append("BANNED_SYMBOLS:\n" + ", ".join(BANNED_SYMBOLS))
    if BANNED_NAME_SUBSTRINGS:
        parts.append("BANNED_NAME_SUBSTRINGS:\n" + ", ".join(BANNED_NAME_SUBSTRINGS))
    if BANNED_WORDS:
        parts.append("BANNED_WORDS:\n" + ", ".join(BANNED_WORDS))
    reply = "\n\n".join(parts) if parts else "Список пуст."
    await update.message.reply_text(reply)

async def addspam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет доступа.")
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Используй: /addspam [тип] [значение]\n"
            "Типы: word, phrase, symbol, fullname, substring, combo\n"
            "Пример: /addspam word NFT"
        )
        return
    typ, value = args[0].strip().lower(), " ".join(args[1:]).strip()
    config = load_config()  # всегда свежий
    edited = False
    if typ == "word":
        if value not in config["BANNED_WORDS"]:
            config["BANNED_WORDS"].append(value)
            edited = True
    elif typ == "phrase":
        if value not in config["PERMANENT_BLOCK_PHRASES"]:
            config["PERMANENT_BLOCK_PHRASES"].append(value)
            edited = True
    elif typ == "symbol":
        if value not in config["BANNED_SYMBOLS"]:
            config["BANNED_SYMBOLS"].append(value)
            edited = True
    elif typ == "fullname":
        if value not in config["BANNED_FULL_NAMES"]:
            config["BANNED_FULL_NAMES"].append(value)
            edited = True
    elif typ == "substring":
        if value not in config["BANNED_NAME_SUBSTRINGS"]:
            config["BANNED_NAME_SUBSTRINGS"].append(value)
            edited = True
    elif typ == "combo":
        words = value.split()
        if words and words not in config["COMBINED_BLOCKS"]:
            config["COMBINED_BLOCKS"].append(words)
            edited = True
    if edited:
        save_config(config)
        await update.message.reply_text(f"Добавлено в {typ}: {value}")
    else:
        await update.message.reply_text(f"Такое уже есть или неизвестный тип.")

# --- Запуск ---
async def init_app():
    port  = int(os.environ.get("PORT", 8443))
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    base = os.getenv("WEBHOOK_URL") or f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"
    webhook_url = f"{base}/webhook"
    print("🔗 Webhook:", webhook_url)

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("spamlist", spamlist))
    app.add_handler(CommandHandler("addspam", addspam))
    app.add_handler(MessageHandler(filters.ALL, delete_spam_message))

    await app.initialize()
    await app.bot.set_webhook(webhook_url)

    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="OK"))
    web_app.router.add_post("/webhook", lambda r: handle_webhook(r, app))
    return web_app, port

async def handle_webhook(request, app):
    data   = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(text="OK")

async def main():
    web_app, port = await init_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site   = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🚀 Running on port {port}")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
