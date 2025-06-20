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
from telegram import Update, ChatPermissions, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler, ConversationHandler
)
import pymorphy2

# --- Fix для pymorphy2 на Python 3.11+ ---
ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")
def fix_getargspec(func):
    spec = inspect.getfullargspec(func)
    return ArgSpec(args=spec.args, varargs=spec.varargs, keywords=spec.varkw, defaults=spec.defaults)
inspect.getargspec = fix_getargspec

morph = pymorphy2.MorphAnalyzer()
nest_asyncio.apply()

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
ADMIN_CHAT_ID = 296920330  # твой id

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

# --- Helpers ---
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

# --- Обработчик сообщений ---
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
    # 0) 💋 в имени — сразу бан
    if "💋" in clean_name:
        print("   ❌ Found 💋 in name, banning immediately")
        ban = True

    # 1) По подстрокам в имени
    if not ban:
        for substr in BANNED_NAME_SUBSTRINGS:
            if normalize_text(substr) in name_lower:
                print(f"   ❌ Substring match in name: {substr}")
                ban = True
                break

    # 2) Точное имя
    if not ban:
        norm_name    = lemmatize_text(name_lower)
        banned_norms = [lemmatize_text(normalize_text(n)) for n in BANNED_FULL_NAMES]
        if norm_name in banned_norms:
            print("   ❌ Full name match")
            ban = True

    # 3) По символам
    if not ban:
        matched = [s for s in BANNED_SYMBOLS if s in clean_name]
        if matched:
            print(f"   ❌ Symbol match: {matched}")
            ban = True

    # 4) По фразам
    if not ban:
        for phrase in PERMANENT_BLOCK_PHRASES:
            if lemmatize_text(normalize_text(phrase)) in proc_text:
                print(f"   ❌ Phrase match: {phrase}")
                ban = True
                break

    # 5) По комбинациям
    if not ban:
        for combo in COMBINED_BLOCKS:
            if all(lemmatize_text(normalize_text(w)) in proc_text for w in combo):
                print(f"   ❌ Combo match: {combo}")
                ban = True
                break

    # 6) По отдельным запрещённым словам
    if not ban:
        for word in BANNED_WORDS:
            if word.lower() in text.lower():
                print(f"   ❌ Word match: {word}")
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

# --- /addspam интерактив ---
ADD_TYPE, ADD_VALUE = range(2)
ADD_TYPES = [
    ("Слово", "BANNED_WORDS"),
    ("Фразу", "PERMANENT_BLOCK_PHRASES"),
    ("Символ", "BANNED_SYMBOLS"),
    ("Имя", "BANNED_FULL_NAMES"),
    ("Подстроку в имени", "BANNED_NAME_SUBSTRINGS"),
    ("Комбинацию (через запятую)", "COMBINED_BLOCKS"),
]
ADD_TYPE_MAP = {str(i+1): t for i, t in enumerate(ADD_TYPES)}

async def addspam_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет доступа.")
        return ConversationHandler.END
    buttons = [[f"{i+1}. {name}"] for i, (name, _) in enumerate(ADD_TYPES)]
    kb = ReplyKeyboardMarkup(buttons, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Что добавить?\n" + "\n".join([f"{i+1}. {name}" for i, (name, _) in enumerate(ADD_TYPES)]), reply_markup=kb)
    return ADD_TYPE

async def addspam_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    num = text.split(".")[0]
    if num not in ADD_TYPE_MAP:
        await update.message.reply_text("Некорректный выбор. Введите номер.")
        return ADD_TYPE
    context.user_data["addspam_type"] = ADD_TYPE_MAP[num][1]
    prompt = "Введи текст:" if num != "6" else "Введи комбинацию слов через запятую:"
    await update.message.reply_text(prompt, reply_markup=ReplyKeyboardRemove())
    return ADD_VALUE

async def addspam_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typ = context.user_data.get("addspam_type")
    val = update.message.text.strip()
    config = load_config()
    if typ == "COMBINED_BLOCKS":
        arr = [lemmatize_text(normalize_text(x.strip())) for x in val.split(",")]
        config[typ].append(arr)
        msg = f"Добавлена комбинация: {arr}"
    else:
        config[typ].append(val)
        msg = f"Добавлено: {val} в {typ}"
    save_config(config)
    await update.message.reply_text(f"✅ {msg}", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def addspam_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

addspam_conv = ConversationHandler(
    entry_points=[CommandHandler("addspam", addspam_start)],
    states={
        ADD_TYPE: [MessageHandler(filters.TEXT & (~filters.COMMAND), addspam_type)],
        ADD_VALUE: [MessageHandler(filters.TEXT & (~filters.COMMAND), addspam_value)],
    },
    fallbacks=[CommandHandler("cancel", addspam_cancel)],
)

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
    app.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    app.add_handler(addspam_conv)

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
