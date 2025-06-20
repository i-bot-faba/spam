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
from telegram import Update, ChatPermissions, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, ConversationHandler
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

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

config = load_config()

BANNED_FULL_NAMES         = config.get("BANNED_FULL_NAMES", [])
PERMANENT_BLOCK_PHRASES   = config.get("PERMANENT_BLOCK_PHRASES", [])
COMBINED_BLOCKS           = config.get("COMBINED_BLOCKS", [])
BANNED_SYMBOLS            = config.get("BANNED_SYMBOLS", [])
BANNED_NAME_SUBSTRINGS    = config.get("BANNED_NAME_SUBSTRINGS", [])
BANNED_WORDS              = config.get("BANNED_WORDS", [])

ADMIN_CHAT_ID = 296920330

# === Helpers ===
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

# === Спам фильтр ===
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

    # 1) Подстроки в имени
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

    # 3) Символы
    if not ban:
        matched = [s for s in BANNED_SYMBOLS if s in clean_name]
        if matched:
            print(f"   ❌ Symbol match: {matched}")
            ban = True

    # 4) Спам-слова
    if not ban:
        for word in BANNED_WORDS:
            if word.lower() in text.lower():
                print(f"   ❌ BANNED_WORD matched: {word}")
                ban = True
                break

    # 5) Фразы
    if not ban:
        for phrase in PERMANENT_BLOCK_PHRASES:
            if lemmatize_text(normalize_text(phrase)) in proc_text:
                print(f"   ❌ Phrase match: {phrase}")
                ban = True
                break

    # 6) Комбинации
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

# === Диалог для добавления спама ===
(
    WAIT_TYPE,
    WAIT_VALUE,
) = range(2)

ADD_OPTIONS = [
    "Слово (в сообщении)",
    "Фразу (в сообщении)",
    "Символ (в имени)",
    "Имя (точное)",
    "Подстроку в имени",
    "Комбинацию слов (через запятую)"
]

async def addspam_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет прав.")
        return ConversationHandler.END
    reply_markup = ReplyKeyboardMarkup(
        [[x] for x in ADD_OPTIONS], one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text(
        "Что добавить в список блокировки?", reply_markup=reply_markup
    )
    return WAIT_TYPE

async def addspam_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in ADD_OPTIONS:
        await update.message.reply_text("Выбери из меню.")
        return WAIT_TYPE
    context.user_data["add_type"] = text
    msg = {
        "Слово (в сообщении)": "Введи слово для блокировки:",
        "Фразу (в сообщении)": "Введи фразу для блокировки:",
        "Символ (в имени)": "Введи символ (например, 💋):",
        "Имя (точное)": "Введи точное имя для блокировки:",
        "Подстроку в имени": "Введи подстроку для блокировки:",
        "Комбинацию слов (через запятую)": "Введи слова через запятую:",
    }
    await update.message.reply_text(msg[text], reply_markup=ReplyKeyboardMarkup([["Отмена"]], one_time_keyboard=True, resize_keyboard=True))
    return WAIT_VALUE

async def addspam_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    if val.lower() == "отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    conf = load_config()
    t = context.user_data.get("add_type")

    if t == "Слово (в сообщении)":
        if val not in conf["BANNED_WORDS"]:
            conf["BANNED_WORDS"].append(val)
    elif t == "Фразу (в сообщении)":
        if val not in conf["PERMANENT_BLOCK_PHRASES"]:
            conf["PERMANENT_BLOCK_PHRASES"].append(val)
    elif t == "Символ (в имени)":
        if val not in conf["BANNED_SYMBOLS"]:
            conf["BANNED_SYMBOLS"].append(val)
    elif t == "Имя (точное)":
        if val not in conf["BANNED_FULL_NAMES"]:
            conf["BANNED_FULL_NAMES"].append(val)
    elif t == "Подстроку в имени":
        if val not in conf["BANNED_NAME_SUBSTRINGS"]:
            conf["BANNED_NAME_SUBSTRINGS"].append(val)
    elif t == "Комбинацию слов (через запятую)":
        words = [w.strip() for w in val.split(",") if w.strip()]
        if words and words not in conf["COMBINED_BLOCKS"]:
            conf["COMBINED_BLOCKS"].append(words)

    save_config(conf)
    await update.message.reply_text(f"Добавлено: {val}\n\nСохранено в config.json.", reply_markup=None)
    return ConversationHandler.END

addspam_conv = ConversationHandler(
    entry_points=[CommandHandler("addspam", addspam_start)],
    states={
        WAIT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addspam_type)],
        WAIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addspam_value)],
    },
    fallbacks=[MessageHandler(filters.Regex("^(Отмена)$"), lambda u, c: ConversationHandler.END)],
    allow_reentry=True
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
