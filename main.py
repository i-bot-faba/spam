# === Патч для совместимости nsfw-detector с новой версией tensorflow_hub ===
import sys, types
import tensorflow as tf
# Хак: обеспечиваем наличие модуля tensorflow_hub.tf_v1.estimator
sys.modules['tensorflow_hub.tf_v1'] = types.ModuleType('tensorflow_hub.tf_v1')
sys.modules['tensorflow_hub.tf_v1'].estimator = tf.estimator
import tensorflow_hub as hub
hub.tf_v1 = tf.compat.v1

import inspect
from collections import namedtuple
import os
import asyncio
import re
from datetime import datetime, timedelta
from io import BytesIO

import nest_asyncio
import pymorphy2
from nsfw_detector import predict
import imagehash
from aiohttp import web
from PIL import Image
import numpy as np
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)
from pymongo import MongoClient

# --- Fix для pymorphy2 на Python 3.11+ ---
ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")
def fix_getargspec(func):
    spec = inspect.getfullargspec(func)
    return ArgSpec(args=spec.args, varargs=spec.varargs, keywords=spec.varkw, defaults=spec.defaults)
inspect.getargspec = fix_getargspec

nest_asyncio.apply()
morph = pymorphy2.MorphAnalyzer()

# --- MongoDB ---
MONGO_URI = os.getenv("MONGODB_URI")
client = MongoClient(MONGO_URI)
db = client["antispam"]
config_col = db["config"]

ADMIN_CHAT_ID = 296920330  # твой id

# --- Загрузка конфигурации ---
def load_config():
    doc = config_col.find_one({"_id": "main"})
    if not doc:
        return {}
    doc.pop("_id", None)
    return doc

def get_tyumen_time():
    return (datetime.utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")

async def send_admin_notification(bot, text: str):
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Ошибка отправки админу:", e)

# --- СПАМ И ФИЛЬТРАЦИЯ ПО АВАТАРУ И ТЕКСТУ ---
async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return

    user = msg.from_user
    cfg = load_config()

    # 0) NSFW-фильтр аватара через nsfw_detector
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count:
            f = await context.bot.get_file(photos.photos[0][-1].file_id)
            bio = BytesIO()
            await f.download_to_memory(out=bio)
            bio.seek(0)

            img = Image.open(bio).convert("RGB").resize((256,256))
            arr = np.expand_dims(np.array(img)/255.0, 0)
            preds = predict.classify_nd(predict.load_model(cfg.get("NSFW_MODEL_PATH", "nsfw_model.h5")), arr)[0]
            if preds.get("porn", 0) >= cfg.get("NSFW_THRESHOLD", 0.6):
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                await send_admin_notification(
                    context.bot,
                    f"Забанен по NSFW-аватару (score={preds.get('porn'):.2f}): @{user.username or user.first_name}"
                )
                return
    except Exception:
        pass

    # 1) pHash-фильтр аватара
    try:
        ph = imagehash.phash(img)
        for bad in cfg.get("BAD_HASHES", []):
            if (ph - imagehash.hex_to_hash(bad)) <= cfg.get("DISTANCE_THRESHOLD", 5):
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                await send_admin_notification(
                    context.bot,
                    f"Забанен по pHash-аватару: @{user.username or user.first_name}"
                )
                return
    except Exception:
        pass

    # 2) Текстовый спам-фильтр
    if not msg.text:
        return

    text = msg.text
    proc_text = " ".join(morph.parse(w)[0].normal_form for w in re.sub(r'[\uFE00-\uFE0F\u200D]', '', text.lower()).split())
    clean_name = (user.first_name or "") + (f" | {user.last_name}" if user.last_name else "")
    name_lower = clean_name.lower()

    ban = False
    # 💋 в имени
    if "💋" in clean_name:
        ban = True
    # подстрока в имени
    for substr in cfg.get("BANNED_NAME_SUBSTRINGS", []):
        if substr.lower() in name_lower:
            ban = True
            break
    # точное имя
    if not ban:
        norm = " ".join(morph.parse(w)[0].normal_form for w in name_lower.split())
        banned_norms = [morph.parse(n.lower())[0].normal_form for n in cfg.get("BANNED_FULL_NAMES", [])]
        if norm in banned_norms:
            ban = True
    # подстрока в username
    if not ban and user.username:
        for substr in cfg.get("BANNED_USERNAME_SUBSTRINGS", []):
            if substr.lower() in user.username.lower():
                ban = True
                break
    # символы
    if not ban and any(s in clean_name for s in cfg.get("BANNED_SYMBOLS", [])):
        ban = True
    # слова и фразы
    if not ban:
        for w in cfg.get("BANNED_WORDS", []):
            if w.lower() in text.lower():
                ban = True
                break
    if not ban:
        for phrase in cfg.get("PERMANENT_BLOCK_PHRASES", []):
            if phrase.lower() in proc_text:
                ban = True
                break
    # комбинации
    if not ban:
        for combo in cfg.get("COMBINED_BLOCKS", []):
            if all(w.lower() in proc_text for w in combo):
                ban = True
                break

    if ban:
        try: await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except: pass
        try: await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
        except: pass
        await send_admin_notification(
            context.bot,
            f"Забанен: @{user.username or user.first_name}\n"
            f"Имя: {clean_name}\n"
            f"Дата: {get_tyumen_time()}\n"
            f"Сообщение: {text}"
        )

# --- Хендлеры запуска и webhook ---
async def init_app():
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("spamlist", spamlist))
    app.add_handler(MessageHandler(filters.ALL, delete_spam_message))

    await app.initialize()
    await app.bot.set_webhook(webhook_url)

    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="OK"))

    async def handle_webhook(request):
        data = await request.json()
        upd = Update.de_json(data, app.bot)
        await app.process_update(upd)
        return web.Response(text="OK")

    web_app.router.add_post("/webhook", handle_webhook)
    return web_app, int(os.getenv("PORT", 8443))

if __name__ == "__main__":
    web_app, port = asyncio.get_event_loop().run_until_complete(init_app())
    runner = web.AppRunner(web_app)
    asyncio.get_event_loop().run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", port)
    asyncio.get_event_loop().run_until_complete(site.start())
    print(f"🚀 Running on port {port}")
    asyncio.get_event_loop().run_forever()
