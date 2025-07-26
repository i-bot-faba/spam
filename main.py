#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
import inspect
from collections import namedtuple
from datetime import datetime, timedelta
from io import BytesIO

import nest_asyncio
import pymorphy2
import requests
from aiohttp import web
from PIL import Image
import numpy as np
import imagehash
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes, ConversationHandler
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

# --- ОСТАЛЬНОЕ ---

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return

    user = msg.from_user
    cfg_global = load_config()

    # 0) ФИЛЬТР ПО АВАТАРУ: NSFW
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count:
            f = await context.bot.get_file(photos.photos[0][-1].file_id)
            bio = BytesIO()
            await f.download_to_memory(out=bio)
            bio.seek(0)
            img = Image.open(bio).convert("RGB").resize((256,256))
            arr = np.expand_dims(np.array(img)/255.0, 0)
            preds = predict.classify_nd(nsfw_model, arr)[0]
            if preds.get("porn", 0) >= cfg_global.get("NSFW_THRESHOLD", NSFW_THRESHOLD):
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                await send_admin_notification(context.bot,
                    f"Забанен по NSFW-аватару: @{user.username or user.first_name}")
                return
    except Exception:
        pass

    # 1) ФИЛЬТР ПО АВАТАРУ: pHash
    try:
        if photos.total_count:
            phash = imagehash.phash(img)
            for bad in cfg_global.get("BAD_HASHES", BAD_HASHES):
                if (phash - imagehash.hex_to_hash(bad)) <= cfg_global.get("DISTANCE_THRESHOLD", DISTANCE_THRESHOLD):
                    await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                    await send_admin_notification(context.bot,
                        f"Забанен по pHash-аватару: @{user.username or user.first_name}")
                    return
    except Exception:
        pass

    # --- ДАЛЬНЕЙШИЙ СПАМ-ФИЛЬТР ПО ТЕКСТУ ---
    if not msg.text:
        return

    text = msg.text
    proc_text = lemmatize_text(normalize_text(text))

    full_name = user.first_name or ""
    if user.last_name:
        full_name += " | " + user.last_name
    clean_name = re.sub(r'[\uFE00-\uFE0F\u200D]', '', full_name)
    name_lower = normalize_text(clean_name)

    ban = False
    # 💋 в имени
    if "💋" in clean_name:
        ban = True
    # Подстрока в имени
    if not ban:
        for substr in cfg_global.get("BANNED_NAME_SUBSTRINGS", []):
            if normalize_text(substr) in name_lower:
                ban = True; break
    # Точное имя
    if not ban:
        norm_name = lemmatize_text(name_lower)
        banneds = [lemmatize_text(normalize_text(n)) for n in cfg_global.get("BANNED_FULL_NAMES", [])]
        if norm_name in banneds:
            ban = True
    # Подстрока в username
    if not ban and user.username:
        uname = normalize_text(user.username)
        for substr in cfg_global.get("BANNED_USERNAME_SUBSTRINGS", []):
            if normalize_text(substr) in uname:
                ban = True; break
    # Символы
    if not ban:
        if any(s in clean_name for s in cfg_global.get("BANNED_SYMBOLS", [])):
            ban = True
    # По словам
    if not ban:
        for word in cfg_global.get("BANNED_WORDS", []):
            if word.lower() in text.lower():
                ban = True; break
    # Фразы
    if not ban:
        for phrase in cfg_global.get("PERMANENT_BLOCK_PHRASES", []):
            if lemmatize_text(normalize_text(phrase)) in proc_text:
                ban = True; break
    # Комбинации
    if not ban:
        for combo in cfg_global.get("COMBINED_BLOCKS", []):
            if all(lemmatize_text(normalize_text(w)) in proc_text for w in combo):
                ban = True; break

    if ban:
        try: await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except: pass
        try: await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
        except: pass
        notif = (f"Забанен: @{user.username or user.first_name}\n"
                 f"Имя: {clean_name}\n"
                 f"Дата: {get_tyumen_time()}\n"
                 f"Сообщение: {text}")
        await send_admin_notification(context.bot, notif)

# --- остальные хендлеры без изменений ---
# реализация spamlist, addspam_conv и т.д.

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
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
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
