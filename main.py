import inspect
from collections import namedtuple
import os
import asyncio
import re
import hashlib
import emoji
import regex
import nest_asyncio
from time import time as now_ts
from datetime import datetime, timedelta
from io import BytesIO

from aiohttp import web
from PIL import Image
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram import BotCommand

import pymorphy2
from pymongo import MongoClient

# ===================== Константы / глобалки =====================

# Чаты, где проверка аватарки отключена
EXCLUDE_NSFW_CHAT_IDS = {-1001497970298}

# Кэш результата skin-ratio для аватарок: user_id -> (ts, ratio)
AVATAR_CACHE: dict[int, tuple[float, float]] = {}
AVATAR_NSFW_TTL = 24 * 3600  # 24h

PHRASE_HASH_MAP: dict[str, str] = {}

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
    resize_keyboard=True,
    one_time_keyboard=False,
)

# ===================== Утилиты =====================

def is_spam_like(phrase: str, banned_words: list[str], stop_phrases: list[str]) -> bool:
    words = set((phrase or "").lower().split())
    for bw in banned_words:
        if bw.lower() in words:
            return True
    for sp in stop_phrases:
        if sp.lower() in (phrase or "").lower():
            return True
    return False

def is_only_emojis(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    return all(ch in emoji.EMOJI_DATA for ch in stripped)

def get_tyumen_time():
    return (datetime.utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")

def normalize_text(text: str) -> str:
    mapping = {'a':'а','c':'с','e':'е','o':'о','p':'р','y':'у','x':'х','3':'з','0':'о'}
    return "".join(mapping.get(ch, ch) for ch in (text or "").lower())

def lemmatize_text(text: str) -> str:
    return " ".join(morph.parse(w)[0].normal_form for w in (text or "").split())

async def send_admin_notification(bot, text: str):
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Ошибка отправки админу:", e)

def load_config():
    doc = config_col.find_one({"_id": "main"})
    if doc:
        doc.pop("_id", None)
        return doc
    return {
        "BANNED_FULL_NAMES": [],
        "PERMANENT_BLOCK_PHRASES": [],
        "COMBINED_BLOCKS": [],
        "BANNED_SYMBOLS": [],
        "BANNED_NAME_SUBSTRINGS": [],
        "BANNED_WORDS": [],
        "BANNED_USERNAME_SUBSTRINGS": [],
    }

def save_config(cfg):
    config_col.replace_one({"_id": "main"}, {**cfg, "_id": "main"}, upsert=True)

def add_banned_message(text: str):
    banned_col.insert_one({"text": text, "time": datetime.utcnow()})

def analyze_banned_messages(cfg, min_count=2):
    all_msgs = [doc.get("text", "") for doc in banned_col.find()]
    words = []
    for msg in all_msgs:
        words += re.findall(r'\b[\w\d\-\_]+\b', (msg or "").lower())
    stop_words = set(map(str.lower, cfg.get("BANNED_WORDS", [])))
    freq = {}
    for w in words:
        if w in stop_words or len(w) < 4:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, c in freq.items() if c >= min_count]

# ===================== Аватары: skin-ratio =====================

async def _fetch_biggest_avatar(user_id: int, bot):
    photos = await bot.get_user_profile_photos(user_id, limit=1)
    if not photos or photos.total_count == 0:
        return None
    file_id = photos.photos[0][-1].file_id  # самый крупный размер
    f = await bot.get_file(file_id)
    buf = BytesIO()
    await f.download_to_memory(out=buf)
    buf.seek(0)
    return buf

def _skin_ratio(img: Image.Image) -> float:
    small = img.resize((256, 256)).convert("YCbCr")
    pixels = small.getdata()
    total = len(pixels)
    skin = 0
    for (y, cb, cr) in pixels:
        if 80 <= cb <= 135 and 135 <= cr <= 180:
            skin += 1
    return skin / max(total, 1)

async def avatar_skin_ratio(user_id: int, bot) -> float:
    """
    Возвращает долю 'кожи' на аватарке (0..1). Любые ошибки -> 0.0
    Результат кэшируется на 24 часа.
    """
    ts = now_ts()
    cached = AVATAR_CACHE.get(user_id)
    if cached and (ts - cached[0] < AVATAR_NSFW_TTL):
        return cached[1]
    try:
        bio = await _fetch_biggest_avatar(user_id, bot)
        if not bio:
            AVATAR_CACHE[user_id] = (ts, 0.0)
            return 0.0
        img = Image.open(bio).convert("RGB")
        ratio = _skin_ratio(img)
        AVATAR_CACHE[user_id] = (ts, ratio)
        return ratio
    except Exception as e:
        print("avatar_skin_ratio error:", e)
        AVATAR_CACHE[user_id] = (ts, 0.0)
        return 0.0

def avatar_decision(ratio: float) -> str:
    """
    'hard' | 'soft' | 'ok'
    Пороги можно подстроить под свою группу.
    """
    HARD = 0.58
    SOFT = 0.42
    if ratio >= HARD:
        return "hard"
    if ratio >= SOFT:
        return "soft"
    return "ok"

# ===================== Хэндлеры команд =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Для меню нажми кнопку ниже или напиши /menu",
        reply_markup=menu_keyboard
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 <b>Доступные команды:</b>\n"
        "/addspam — добавить спам-слово/фразу\n"
        "/spamlist — показать текущий стоп-лист\n"
        "/analyzeone — анализировать сообщение\n"
        "/analyze — анализ часто встречающихся слов\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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

# --- /analyzeone: множественный выбор фраз с хэшами ---
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
    PHRASE_HASH_MAP.clear()
    for c in candidates:
        sh = hashlib.sha1(c.encode()).hexdigest()[:8]
        PHRASE_HASH_MAP[sh] = c
        keyboard.append([InlineKeyboardButton(text=f"☐ {c}", callback_data=f"toggle_{sh}_0")])
    keyboard.append([InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_phrases")])

    await update.message.reply_text(
        "Выбери фразы для добавления в стоп-лист (можно несколько):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["selected_phrases"] = set()

async def select_phrase_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data.startswith("toggle_"):
        parts = data.split("_")
        sh = parts[1]
        selected_flag = parts[2] == "1"

        selected = context.user_data.get("selected_phrases", set())
        if not selected_flag:
            selected.add(sh)
        else:
            selected.discard(sh)
        context.user_data["selected_phrases"] = selected

        keyboard = []
        for k, phrase in PHRASE_HASH_MAP.items():
            checked = "☑️" if k in selected else "☐"
            cb_sel = "1" if k in selected else "0"
            keyboard.append([InlineKeyboardButton(text=f"{checked} {phrase}", callback_data=f"toggle_{k}_{cb_sel}")])
        keyboard.append([InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_phrases")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "confirm_phrases":
        selected = context.user_data.get("selected_phrases", set())
        if not selected:
            await query.answer("Ничего не выбрано.")
            return
        cfg = load_config()
        to_add = [PHRASE_HASH_MAP[s] for s in selected if s in PHRASE_HASH_MAP]
        for phrase in to_add:
            if phrase not in cfg.get("PERMANENT_BLOCK_PHRASES", []):
                cfg.setdefault("PERMANENT_BLOCK_PHRASES", []).append(phrase)
        save_config(cfg)
        await query.edit_message_text("Фразы добавлены:\n" + "\n".join(to_add))
        context.user_data["selected_phrases"] = set()
        return

# ===================== Хэндлеры антиспама =====================

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return

    # 0) Сообщение только из эмодзи — удаляем
    if msg.text and is_only_emojis(msg.text):
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception:
            pass
        return

    user = msg.from_user
    text = msg.text or ""
    cfg = load_config()
    proc_text = lemmatize_text(normalize_text(text))

    # 1) Проверка аватара (кроме исключённых чатов)
    if msg.chat and msg.chat.id not in EXCLUDE_NSFW_CHAT_IDS:
        try:
            ratio = await avatar_skin_ratio(user.id, context.bot)
            decision = avatar_decision(ratio)
            if decision == "hard":
                try:
                    await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
                except Exception:
                    pass
                try:
                    await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                except Exception:
                    pass
                await send_admin_notification(
                    context.bot,
                    f"БАН по аватарке (skin_ratio={ratio:.2f}): @{user.username or user.first_name} ({user.id})"
                )
                return
            elif decision == "soft":
                await send_admin_notification(
                    context.bot,
                    f"ПРЕДУПРЕЖДЕНИЕ по аватарке (skin_ratio={ratio:.2f}): @{user.username or user.first_name} ({user.id})"
                )
        except Exception as e:
            print("avatar check in message failed:", e)

    # 2) Ник с одинаковыми эмодзи по краям -> бан
    name = (user.first_name or "") + (f" {user.last_name}" if user.last_name else "")
    match = regex.match(r"^(?P<emoji>\X)\s?.+\s?(?P=emoji)$", name, flags=regex.UNICODE)
    if match and len(match.group("emoji")) > 0:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception:
            pass
        try:
            await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
        except Exception:
            pass
        return

    # 3) Остальные правила из конфига
    full_name = (user.first_name or "") + (f" | {user.last_name}" if user.last_name else "")
    clean_name = re.sub(r'[\uFE00-\uFE0F\u200D]', '', full_name)
    name_lower = normalize_text(clean_name)

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
        banned_norms = [lemmatize_text(normalize_text(n)) for n in cfg.get("BANNED_FULL_NAMES", [])]
        if norm_name in banned_norms:
            ban = True

    if not ban and user.username:
        username_lower = normalize_text(user.username)
        for substr in cfg.get("BANNED_USERNAME_SUBSTRINGS", []):
            if normalize_text(substr) in username_lower:
                ban = True
                break

    if not ban:
        matched = [s for s in cfg.get("BANNED_SYMBOLS", []) if s in clean_name]
        if matched:
            ban = True

    def clean_for_match(s: str) -> str:
        return re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', (s or '').lower())

    text_raw_clean = clean_for_match(text)
    if not ban:
        for w in cfg.get("BANNED_WORDS", []):
            if clean_for_match(w) in text_raw_clean:
                ban = True
                break

    if not ban:
        for phrase in cfg.get("PERMANENT_BLOCK_PHRASES", []):
            if lemmatize_text(normalize_text(phrase)) in proc_text:
                ban = True
                break

    if not ban:
        for combo in cfg.get("COMBINED_BLOCKS", []):
            if all(lemmatize_text(normalize_text(w)) in proc_text for w in combo):
                ban = True
                break

    if ban:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception:
            pass
        try:
            await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
        except Exception:
            pass
        notif = (
            f"Забанен: @{user.username or user.first_name}\n"
            f"Имя: {clean_name}\n"
            f"Дата: {get_tyumen_time()}\n"
            f"Сообщение: {text}"
        )
        await send_admin_notification(context.bot, notif)
        add_banned_message(text)

# Новые участники
async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    for u in update.message.new_chat_members:
        if chat_id in EXCLUDE_NSFW_CHAT_IDS:
            continue
        try:
            ratio = await avatar_skin_ratio(u.id, context.bot)
            decision = avatar_decision(ratio)
            if decision == "hard":
                try:
                    await context.bot.ban_chat_member(chat_id, u.id)
                except Exception:
                    pass
                await send_admin_notification(
                    context.bot,
                    f"БАН при входе по аватарке (skin_ratio={ratio:.2f}): @{u.username or u.first_name} ({u.id})"
                )
            elif decision == "soft":
                await send_admin_notification(
                    context.bot,
                    f"ПРЕДУПРЕЖДЕНИЕ при входе (skin_ratio={ratio:.2f}): @{u.username or u.first_name} ({u.id})"
                )
        except Exception as e:
            print("new member avatar check error:", e)

# ===================== addspam диалог =====================

(ADD_CHOICE, ADD_INPUT, ADD_COMBO) = range(3)

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
        "Введи подстроку для поиска в username:",
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
    per_user=True,
)

# ===================== Прочее =====================

async def addword_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    word = (query.data or "").replace("addword_", "")
    cfg = load_config()
    if word and word not in cfg.get("BANNED_WORDS", []):
        cfg.setdefault("BANNED_WORDS", []).append(word)
        save_config(cfg)
        await query.answer("Слово добавлено!")
        await query.edit_message_text(f"Слово добавлено: {word}")
    else:
        await query.answer("Уже в списке!")

async def set_commands(app):
    commands = [
        BotCommand("addspam", "Добавить спам-слово/фразу"),
        BotCommand("spamlist", "Показать текущий стоп-лист"),
        BotCommand("analyzeone", "Анализировать сообщение"),
        BotCommand("start", "Информация о боте"),
    ]
    await app.bot.set_my_commands(commands)

# ===================== Веб-сервер / запуск =====================

async def handle_webhook(request, app):
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response(text="OK")

async def init_app():
    port  = int(os.environ.get("PORT", 8443))
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    base = os.getenv("WEBHOOK_URL") or f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"
    webhook_url = f"{base}/webhook"
    print("🔗 Webhook:", webhook_url)

    app = ApplicationBuilder().token(TOKEN).build()
    # Команды/диалоги
    app.add_handler(addspam_conv)
    app.add_handler(CommandHandler("spamlist", spamlist))
    app.add_handler(CommandHandler("analyze", analyze_banned))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyzeone", analyzeone))

    # Коллбэки кнопок выбора фраз
    app.add_handler(CallbackQueryHandler(select_phrase_callback, pattern="^(toggle_|confirm_phrases)"))
    app.add_handler(CallbackQueryHandler(addword_callback, pattern=r"^addword_"))

    # Новые участники
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member))

    # Антиспам (ставь в самый низ!)
    app.add_handler(MessageHandler(filters.ALL, delete_spam_message))

    await app.initialize()
    await set_commands(app)
    await app.bot.set_webhook(webhook_url)

    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="OK"))
    web_app.router.add_post("/webhook", lambda r: handle_webhook(r, app))
    return web_app, port

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
