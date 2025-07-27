import inspect
from collections import namedtuple
import os
import asyncio
import re
import nest_asyncio
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes, ConversationHandler
)
import pymorphy2
from pymongo import MongoClient
import requests
from io import BytesIO
from PIL import Image
import imagehash

# --- Fix для pymorphy2 на Python 3.11+ ---
ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")
def fix_getargspec(func):
    spec = inspect.getfullargspec(func)
    return ArgSpec(args=spec.args, varargs=spec.varargs, keywords=spec.varkw, defaults=spec.defaults)
inspect.getargspec = fix_getargspec

morph = pymorphy2.MorphAnalyzer()
nest_asyncio.apply()

# --- MongoDB ---
MONGO_URI = os.getenv("MONGODB_URI")
client = MongoClient(MONGO_URI)
db = client["antispam"]
config_col = db["config"]

ADMIN_CHAT_ID = 296920330  # твой id

def load_config():
    doc = config_col.find_one({"_id": "main"})
    if doc:
        doc.pop("_id")
        return doc
    return {
        "BANNED_FULL_NAMES": [],
        "PERMANENT_BLOCK_PHRASES": [],
        "COMBINED_BLOCKS": [],
        "BANNED_SYMBOLS": [],
        "BANNED_NAME_SUBSTRINGS": [],
        "BANNED_WORDS": []
    }

def save_config(cfg):
    config_col.replace_one({"_id": "main"}, {**cfg, "_id": "main"}, upsert=True)

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

# --- СПАМ ХЕНДЛЕР ---
async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    user = msg.from_user
    cfg = load_config()
    text = msg.text
    proc_text = lemmatize_text(normalize_text(text))

    full_name = user.first_name or ""
    if user.last_name:
        full_name += " | " + user.last_name
    clean_name = re.sub(r'[\uFE00-\uFE0F\u200D]', '', full_name)
    name_lower = normalize_text(clean_name)

    # --- NSFW и pHash фильтрация аватарки ---
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count:
            f = await context.bot.get_file(photos.photos[0][-1].file_id)
            bio = BytesIO()
            await f.download_to_memory(out=bio)
            bio.seek(0)
            img = Image.open(bio).convert("RGB")
            
            # NSFW DeepAI
            resp = requests.post(
                "https://api.deepai.org/api/nsfw-detector",
                files={"image": bio.getvalue()},
                headers={"api-key": os.getenv("DEEPAI_API_KEY")}
            )
            score = resp.json().get("output", {}).get("nsfw_score", 0)
            print(f"NSFW check: user={user.id}, score={score}")

            with open("nsfw_log.txt", "a") as logf:
                logf.write(f"{datetime.utcnow()} user={user.id} score={score}\n")

            if score >= cfg.get("NSFW_THRESHOLD", 0.6):
                await context.bot.ban_chat_member(msg.chat.id, user.id)
                await send_admin_notification(
                    context.bot,
                    f"Забанен по NSFW-аватару (score={score:.2f}): @{user.username or user.first_name}"
                )
                return

            # pHash
            ph = imagehash.phash(img)
            for bad in cfg.get("BAD_HASHES", []):
                if (ph - imagehash.hex_to_hash(bad)) <= cfg.get("DISTANCE_THRESHOLD", 5):
                    await context.bot.ban_chat_member(msg.chat.id, user.id)
                    await send_admin_notification(
                        context.bot,
                        f"Забанен по pHash-аватару: @{user.username or user.first_name}"
                    )
                    return
    except Exception as ex:
        print(f"Ошибка NSFW/pHash: {ex}")

    # --- Проверки имени, username, текста, комбинаций ---
    ban = False
    if "💋" in clean_name:
        ban = True

    if not ban:
        for substr in cfg.get("BANNED_NAME_SUBSTRINGS", []):
            if normalize_text(substr) in name_lower:
                ban = True
                break

    if not ban:
        norm_name = lemmatize_text(name_lower)
        banned_norms = [lemmatize_text(normalize_text(n)) for n in cfg.get("BANNED_FULL_NAMES",[])]
        if norm_name in banned_norms:
            ban = True

    if not ban and user.username:
        username_lower = normalize_text(user.username)
        for substr in cfg.get("BANNED_USERNAME_SUBSTRINGS", []):
            if normalize_text(substr) in username_lower:
                ban = True
                break

    if not ban:
        matched = [s for s in cfg.get("BANNED_SYMBOLS",[]) if s in clean_name]
        if matched:
            ban = True

    if not ban:
        for word in cfg.get("BANNED_WORDS",[]):
            if word.lower() in text.lower():
                ban = True
                break

    if not ban:
        for phrase in cfg.get("PERMANENT_BLOCK_PHRASES",[]):
            if lemmatize_text(normalize_text(phrase)) in proc_text:
                ban = True
                break

    if not ban:
        for combo in cfg.get("COMBINED_BLOCKS",[]):
            if all(lemmatize_text(normalize_text(w)) in proc_text for w in combo):
                ban = True
                break

    if ban:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception: pass
        try:
            await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
        except Exception: pass
        notif = (
            f"Забанен: @{user.username or user.first_name}\n"
            f"Имя: {clean_name}\n"
            f"Дата: {get_tyumen_time()}\n"
            f"Сообщение: {text}"
        )
        await send_admin_notification(context.bot, notif)

# --- /SPAMLIST ---
async def spamlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет доступа.")
        return
    cfg = load_config()
    text = (
        "<b>BANNED_WORDS</b>:\n" + "\n".join(cfg.get("BANNED_WORDS", [])) + "\n\n"
        "<b>BANNED_FULL_NAMES</b>:\n" + "\n".join(cfg.get("BANNED_FULL_NAMES", [])) + "\n\n"
        "<b>BANNED_SYMBOLS</b>:\n" + " ".join(cfg.get("BANNED_SYMBOLS", [])) + "\n\n"
        "<b>BANNED_NAME_SUBSTRINGS</b>:\n" + "\n".join(cfg.get("BANNED_NAME_SUBSTRINGS", [])) + "\n\n"
        "<b>PERMANENT_BLOCK_PHRASES</b>:\n" + "\n".join(cfg.get("PERMANENT_BLOCK_PHRASES", [])) + "\n\n"
        "<b>COMBINED_BLOCKS</b>:\n" + "\n".join([', '.join(block) for block in cfg.get("COMBINED_BLOCKS", [])])
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# --- /ADDSPAM ---
(
    ADD_CHOICE, ADD_INPUT, ADD_COMBO
) = range(3)

async def addspam_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет доступа.")
        return ConversationHandler.END
    await update.message.reply_text(
        "Что добавить?\n"
        "1️⃣ Слово\n"
        "2️⃣ Фразу\n"
        "3️⃣ Символ\n"
        "4️⃣ Имя\n"
        "5️⃣ Подстроку в имени\n"
        "6️⃣ Комбинацию слов (через запятую)\n"
        "7️⃣ Подстроку в username (никнейме)\n\n"
        "Отправь номер (1-7):"
    )
    return ADD_CHOICE  # <-- ДОЛЖНО БЫТЬ С ОТСТУПОМ
    
async def addspam_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice not in "1234567":
        await update.message.reply_text("Введи число от 1 до 7.")
        return ADD_CHOICE
    context.user_data["addspam_type"] = int(choice)
    if choice == "6":
        await update.message.reply_text("Введи слова через запятую (пример: трейдинг, инвестиции, криптовалюты):")
        return ADD_COMBO
    prompts = [
        "Введи слово:",
        "Введи фразу:",
        "Введи символ:",
        "Введи имя полностью:",
        "Введи подстроку для поиска в имени:",
        "",  # для шестого пункта
        "Введи подстроку для поиска в username:"
    ]
    await update.message.reply_text(prompts[int(choice)-1])
    return ADD_INPUT

async def addspam_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    spam_type = context.user_data["addspam_type"]
    cfg = load_config()
    if spam_type == 1:
        cfg.setdefault("BANNED_WORDS", []).append(value)
        await update.message.reply_text(f"Слово добавлено: {value}")
    elif spam_type == 2:
        cfg.setdefault("PERMANENT_BLOCK_PHRASES", []).append(value)
        await update.message.reply_text(f"Фраза добавлена: {value}")
    elif spam_type == 3:
        cfg.setdefault("BANNED_SYMBOLS", []).append(value)
        await update.message.reply_text(f"Символ добавлен: {value}")
    elif spam_type == 4:
        cfg.setdefault("BANNED_FULL_NAMES", []).append(value)
        await update.message.reply_text(f"Имя добавлено: {value}")
    elif spam_type == 5:
        cfg.setdefault("BANNED_NAME_SUBSTRINGS", []).append(value)
        await update.message.reply_text(f"Подстрока в имени добавлена: {value}")
    elif spam_type == 7:
        cfg.setdefault("BANNED_USERNAME_SUBSTRINGS", []).append(value)
        await update.message.reply_text(f"Подстрока в username добавлена: {value}")
    save_config(cfg)
    return ConversationHandler.END

async def addspam_combo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    words = [w.strip() for w in text.split(",") if w.strip()]
    if len(words) < 2:
        await update.message.reply_text("Нужно минимум два слова через запятую.")
        return ADD_COMBO
    cfg = load_config()
    cfg.setdefault("COMBINED_BLOCKS", []).append(words)
    save_config(cfg)
    await update.message.reply_text(f"Комбинация добавлена: {', '.join(words)}")
    return ConversationHandler.END

# --- ФОЛБЭК ДЛЯ ЛЮБОЙ КОМАНДЫ ---
async def cancel_addspam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Добавление спама отменено.")
    return ConversationHandler.END

addspam_conv = ConversationHandler(
    entry_points=[CommandHandler("addspam", addspam_start)],
    states={
        ADD_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addspam_choice)],
        ADD_INPUT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addspam_input)],
        ADD_COMBO:  [MessageHandler(filters.TEXT & ~filters.COMMAND, addspam_combo)],
    },
    fallbacks=[MessageHandler(filters.COMMAND, cancel_addspam)],
    per_user=True
)

# --- ЗАПУСК ---
async def init_app():
    port  = int(os.environ.get("PORT", 8443))
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    base = os.getenv("WEBHOOK_URL") or f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"
    webhook_url = f"{base}/webhook"
    print("🔗 Webhook:", webhook_url)
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(addspam_conv)
    app.add_handler(CommandHandler("spamlist", spamlist))
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
