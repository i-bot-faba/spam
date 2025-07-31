import inspect
from collections import namedtuple
import os
import asyncio
import re
import hashlib
import nest_asyncio
from telegram import ReplyKeyboardMarkup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)
import pymorphy2
from pymongo import MongoClient
import requests
from io import BytesIO
from PIL import Image
import imagehash

PHRASE_HASH_MAP = {}candidates

ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")
def fix_getargspec(func):
    spec = inspect.getfullargspec(func)
    return ArgSpec(args=spec.args, varargs=spec.varargs, keywords=spec.varkw, defaults=spec.defaults)
inspect.getargspec = fix_getargspec

morph = pymorphy2.MorphAnalyzer()
nest_asyncio.apply()

MONGO_URI = os.getenv("MONGODB_URI")
client = MongoClient(MONGO_URI)
db = client["antispam"]
config_col = db["config"]
banned_col = db["banned_messages"]

ADMIN_CHAT_ID = 296920330

menu_keyboard = ReplyKeyboardMarkup(
    [["/addspam", "/spamlist"], ["/analyze", "/analyzeone"]],
    resize_keyboard=True, one_time_keyboard=False
)

def is_spam_like(phrase, banned_words, stop_phrases):
    words = set(phrase.lower().split())
    for bw in banned_words:
        if bw.lower() in words:
            return True
    for sp in stop_phrases:
        if sp.lower() in phrase.lower():
            return True
    return False
    
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Для меню нажми кнопку ниже или напиши /menu",
        reply_markup=menu_keyboard
    )

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
        "BANNED_WORDS": [],
        "BANNED_USERNAME_SUBSTRINGS": []
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

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 <b>Доступные команды:</b>\n"
        "/addspam — добавить спам-слово/фразу\n"
        "/spamlist — показать текущий стоп-лист\n"
        "/analyzeone — анализировать сообщение\n"
        "/analyze — анализ часто встречающихся слов\n"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML
    )

# --- Анализ новых банов для автопредложения ---
def add_banned_message(text):
    banned_col.insert_one({
        "text": text,
        "time": datetime.utcnow()
    })

def analyze_banned_messages(cfg, min_count=2):
    # Собирает слова/фразы, которые чаще всего встречаются в забаненных сообщениях, но не входят в стоп-лист
    all_msgs = [doc["text"] for doc in banned_col.find()]
    words = []
    for msg in all_msgs:
        words += re.findall(r'\b[\w\d\-\_]+\b', msg.lower())
    stop_words = set(map(str.lower, cfg.get("BANNED_WORDS", [])))
    freq = {}
    for w in words:
        if w in stop_words or len(w) < 4:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, c in freq.items() if c >= min_count]

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

    ban = False
    # все проверки как раньше
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

    # --- Блок по вхождению даже с символами (расширенный) ---
    def clean_for_match(s):
        # Удаляем спецсимволы и приводим к нижнему
        return re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', s.lower())

    text_raw_clean = clean_for_match(text)
    for w in cfg.get("BANNED_WORDS",[]):
        if clean_for_match(w) in text_raw_clean:
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
        # --- Сохраняем забаненные сообщения для автоанализа ---
        add_banned_message(text)

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

async def analyze_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет доступа.")
        return
    cfg = load_config()
    candidates = analyze_banned_messages(cfg)
    if not candidates:
        await update.message.reply_text("Нет новых часто встречающихся слов.")
        return
    await update.message.reply_text("Часто встречающиеся новые слова:\n" + "\n".join(candidates))
    
# --- /ANALYZE (анализ новых забаненных для пополнения стоп-листа) ---
async def analyzeone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Нет доступа.")
        return

    if not context.args:
        await update.message.reply_text("Кинь текст для анализа после команды.")
        return

        text = " ".join(context.args)
    cfg = load_config()
    stop_phrases = cfg.get("PERMANENT_BLOCK_PHRASES", [])

    parts = re.split(r"[.,;:\-!?]", text)
    candidates = [
        p.strip() for p in parts
        if len(p.strip()) >= 10 and p.strip() not in stop_phrases
    ]
    if not candidates:
        await update.message.reply_text("Нет подходящих новых фраз для добавления.")
        return

    keyboard = []
    for c in candidates:
        short_hash = hashlib.sha1(c.encode()).hexdigest()[:8]
        PHRASE_HASH_MAP[short_hash] = c  # сохраняем соответствие хэш-фраза
        keyboard.append([InlineKeyboardButton(c, callback_data=f"add_phrase|{short_hash}")])

    await update.message.reply_text(
        "Выбери фразу для добавления в стоп-лист:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return

# обработчик callback для add_phrase
async def add_phrase_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("add_phrase|"):
        short_hash = data.split("|", 1)[1]
        phrase = PHRASE_HASH_MAP.get(short_hash)  # берем фразу по хэшу
        if not phrase:
            await query.edit_message_text("Фраза не найдена.")
            returnphrase
        cfg = load_config()
        if phrase not in cfg.get("PERMANENT_BLOCK_PHRASES", []):
            cfg.setdefault("PERMANENT_BLOCK_PHRASES", []).append(phrase)
            save_config(cfg)
            await query.edit_message_text(f"Фраза добавлена:\n{phrase}")
        else:
            await query.edit_message_text("Фраза уже есть в списке.")

# --- /ADDSPAM как раньше, плюс BANNED_USERNAME_SUBSTRINGS ---
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
    return ADD_CHOICE

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

async def add_phrase_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("add_phrase|"):
        phrase = data.split("|", 1)[1]
        cfg = load_config()
        if phrase not in cfg.get("PERMANENT_BLOCK_PHRASES", []):
            cfg.setdefault("PERMANENT_BLOCK_PHRASES", []).append(phrase)
            save_config(cfg)
            await query.edit_message_text(f"Фраза добавлена:\n{phrase}")
        else:
            await query.edit_message_text("Фраза уже есть в списке.")

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

async def addword_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    word = query.data.replace("addword_", "")
    cfg = load_config()
    if word not in cfg.get("BANNED_WORDS", []):
        cfg.setdefault("BANNED_WORDS", []).append(word)
        save_config(cfg)
        await query.answer("Слово добавлено!")
        await query.edit_message_text(f"Слово добавлено: {word}")
    else:
        await query.answer("Уже в списке!")

from telegram import BotCommand

async def set_commands(app):
    commands = [
        BotCommand("addspam", "Добавить спам-слово/фразу"),
        BotCommand("spamlist", "Показать текущий стоп-лист"),
        BotCommand("analyzeone", "Анализировать сообщение"),
        BotCommand("start", "Информация о боте"),
    ]
    await app.bot.set_my_commands(commands)
    
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
    app.add_handler(CommandHandler("analyze", analyze_banned))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyzeone", analyzeone))
    app.add_handler(CallbackQueryHandler(add_phrase_callback, pattern="^add_phrase\|"))
    app.add_handler(CallbackQueryHandler(addword_callback, pattern=r"^addword_"))
    app.add_handler(MessageHandler(filters.ALL, delete_spam_message))  # <-- СТАВЬ В САМЫЙ НИЗ!
    await app.initialize()
    await set_commands(app)
    await app.bot.set_webhook(webhook_url)
    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="OK"))
    web_app.router.add_post("/webhook", lambda r: handle_webhook(r, app))
    return web_app, port

async def handle_webhook(request, app):
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
    print(f"🚀 Running on port {port}")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
