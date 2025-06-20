import inspect
from collections import namedtuple
import os
import asyncio
import re
import json
import nest_asyncio
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)
import pymorphy2

# --- Fix для pymorphy2 ---
ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")
def fix_getargspec(func):
    spec = inspect.getfullargspec(func)
    return ArgSpec(args=spec.args, varargs=spec.varargs, keywords=spec.varkw, defaults=spec.defaults)
inspect.getargspec = fix_getargspec
morph = pymorphy2.MorphAnalyzer()
nest_asyncio.apply()

# --- Config ---
def config_path():
    return os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    with open(config_path(), "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()
BANNED_FULL_NAMES         = config.get("BANNED_FULL_NAMES", [])
PERMANENT_BLOCK_PHRASES   = config.get("PERMANENT_BLOCK_PHRASES", [])
COMBINED_BLOCKS           = config.get("COMBINED_BLOCKS", [])
BANNED_SYMBOLS            = config.get("BANNED_SYMBOLS", [])
BANNED_NAME_SUBSTRINGS    = config.get("BANNED_NAME_SUBSTRINGS", [])
BANNED_WORDS              = config.get("BANNED_WORDS", [])

ADMIN_CHAT_ID = 296920330

# --- Utils ---
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

def clear_spam_state(context):
    keys = [
        "awaiting_spam_type", "awaiting_spam_value",
        "selected_spam_type", "awaiting_combo"
    ]
    for key in keys:
        context.user_data.pop(key, None)

# --- /spamlist команда ---
async def spamlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        return
    cfg = load_config()
    txt = (
        f"<b>BANNED_WORDS:</b>\n{chr(10).join(cfg.get('BANNED_WORDS', []))}\n\n"
        f"<b>BANNED_FULL_NAMES:</b>\n{chr(10).join(cfg.get('BANNED_FULL_NAMES', []))}\n\n"
        f"<b>PERMANENT_BLOCK_PHRASES:</b>\n{chr(10).join(cfg.get('PERMANENT_BLOCK_PHRASES', []))}\n\n"
        f"<b>BANNED_SYMBOLS:</b>\n{' '.join(cfg.get('BANNED_SYMBOLS', []))}\n\n"
        f"<b>BANNED_NAME_SUBSTRINGS:</b>\n{chr(10).join(cfg.get('BANNED_NAME_SUBSTRINGS', []))}\n\n"
        f"<b>COMBINED_BLOCKS:</b>\n" + "\n".join([", ".join(block) for block in cfg.get('COMBINED_BLOCKS', [])])
    )
    await update.message.reply_text(txt, parse_mode="HTML")
    clear_spam_state(context)

# --- /addspam команда ---
async def addspam_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        return
    clear_spam_state(context)
    context.user_data["awaiting_spam_type"] = True
    await update.message.reply_text(
        "Что добавить?\n"
        "1️⃣ Слово\n"
        "2️⃣ Фразу\n"
        "3️⃣ Символ\n"
        "4️⃣ Имя\n"
        "5️⃣ Подстроку в имени\n"
        "6️⃣ Комбинацию слов (через запятую)\n"
        "Отправь номер (1-6):"
    )

async def addspam_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        return
    # Выбор типа
    if context.user_data.get("awaiting_spam_type"):
        msg = update.message.text.strip()
        typemap = {
            "1": "BANNED_WORDS",
            "2": "PERMANENT_BLOCK_PHRASES",
            "3": "BANNED_SYMBOLS",
            "4": "BANNED_FULL_NAMES",
            "5": "BANNED_NAME_SUBSTRINGS",
            "6": "COMBINED_BLOCKS"
        }
        if msg not in typemap:
            await update.message.reply_text("Введи номер от 1 до 6.")
            return
        spam_type = typemap[msg]
        context.user_data["selected_spam_type"] = spam_type
        context.user_data.pop("awaiting_spam_type", None)
        if spam_type == "COMBINED_BLOCKS":
            context.user_data["awaiting_combo"] = True
            await update.message.reply_text("Введи слова через запятую (например: инвестиции, трейдинг, криптовалюты):")
        else:
            context.user_data["awaiting_spam_value"] = True
            await update.message.reply_text("Введи значение для спам-листа:")
        return

    # Ввод значения
    if context.user_data.get("awaiting_spam_value"):
        value = update.message.text.strip()
        spam_type = context.user_data.get("selected_spam_type")
        cfg = load_config()
        arr = cfg.get(spam_type, [])
        if value in arr:
            await update.message.reply_text("Такое уже есть.")
        else:
            arr.append(value)
            cfg[spam_type] = arr
            save_config(cfg)
            await update.message.reply_text(f"Добавлено в {spam_type}:\n{value}")
        clear_spam_state(context)
        return

    # Ввод комбо
    if context.user_data.get("awaiting_combo"):
        value = update.message.text.strip()
        combo = [w.strip() for w in value.split(",") if w.strip()]
        cfg = load_config()
        if combo in cfg.get("COMBINED_BLOCKS", []):
            await update.message.reply_text("Такая комбинация уже есть.")
        else:
            cfg.setdefault("COMBINED_BLOCKS", []).append(combo)
            save_config(cfg)
            await update.message.reply_text(f"Добавлена комбинация:\n{', '.join(combo)}")
        clear_spam_state(context)
        return

# --- Спам-фильтр (твоя логика, не менял) ---
async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    cfg = load_config()
    text      = msg.text
    proc_text = lemmatize_text(normalize_text(text))
    user      = msg.from_user
    full_name = user.first_name or ""
    if user.last_name:
        full_name += " | " + user.last_name
    clean_name = re.sub(r'[\uFE00-\uFE0F\u200D]', '', full_name)
    name_lower = normalize_text(clean_name)

    ban = False
    # 💋
    if "💋" in clean_name:
        ban = True
    # Подстроки в имени
    if not ban:
        for substr in cfg.get("BANNED_NAME_SUBSTRINGS", []):
            if normalize_text(substr) in name_lower:
                ban = True
                break
    # Точное имя
    if not ban:
        norm_name = lemmatize_text(name_lower)
        banned_norms = [lemmatize_text(normalize_text(n)) for n in cfg.get("BANNED_FULL_NAMES", [])]
        if norm_name in banned_norms:
            ban = True
    # Символы
    if not ban:
        if any(s in clean_name for s in cfg.get("BANNED_SYMBOLS", [])):
            ban = True
    # Слова в тексте
    if not ban:
        for word in cfg.get("BANNED_WORDS", []):
            if word.lower() in text.lower():
                ban = True
                break
    # Фразы
    if not ban:
        for phrase in cfg.get("PERMANENT_BLOCK_PHRASES", []):
            if lemmatize_text(normalize_text(phrase)) in proc_text:
                ban = True
                break
    # Комбинации
    if not ban:
        for combo in cfg.get("COMBINED_BLOCKS", []):
            if all(lemmatize_text(normalize_text(w)) in proc_text for w in combo):
                ban = True
                break

    if ban:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception as e:
            print("Ошибка удаления:", e)
        try:
            await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
        except Exception as e:
            print("Ошибка бана:", e)
        notif = (
            f"Забанен: @{user.username or user.first_name}\n"
            f"Имя: {clean_name}\n"
            f"Дата: {get_tyumen_time()}\n"
            f"Сообщение: {text}"
        )
        await send_admin_notification(context.bot, notif)

# --- Telegram запуск ---
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
    app.add_handler(CommandHandler("addspam", addspam_command))
    app.add_handler(CommandHandler("spamlist", spamlist_command))
    # Обработчик для диалога добавления спама
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), addspam_handler))

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
