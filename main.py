#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# --- Хак для совместимости nsfw-detector с новой версией tensorflow_hub ---
import sys, types
import tensorflow as tf

# создаём модуль tensorflow_hub.tf_v1.estimator
sys.modules['tensorflow_hub.tf_v1'] = types.ModuleType('tensorflow_hub.tf_v1')
sys.modules['tensorflow_hub.tf_v1'].estimator = tf.estimator

import tensorflow_hub as hub
hub.tf_v1 = tf.compat.v1

import inspect
from collections import namedtuple
import os
import asyncio
import re
import nest_asyncio
from datetime import datetime, timedelta
from io import BytesIO

from aiohttp import web
from PIL import Image
import numpy as np
import imagehash
from nsfw_detector import predict

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes, ConversationHandler
)
import pymorphy2
from pymongo import MongoClient

# --- Fix для pymorphy2 на Python 3.11+ ---
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
    if not doc:
        return {}
    doc.pop("_id", None)
    return doc

def save_config(cfg):
    config_col.replace_one({"_id": "main"}, {**cfg, "_id": "main"}, upsert=True)

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

# === Глобальные переменные для модели ===
nsfw_model = None

# --- СПАМ ХЕНДЛЕР ---
async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return

    user = msg.from_user
    cfg = load_config()

    # 0) NSFW-фильтр аватара
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
            if preds.get("porn", 0) >= cfg.get("NSFW_THRESHOLD", 0.6):
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                await send_admin_notification(context.bot,
                    f"Забанен по NSFW-аватару: @{user.username or user.first_name}")
                return
    except Exception:
        pass

    # 1) pHash-фильтр аватара
    try:
        ph = imagehash.phash(img)
        for bad in cfg.get("BAD_HASHES", []):
            if (ph - imagehash.hex_to_hash(bad)) <= cfg.get("DISTANCE_THRESHOLD", 5):
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                await send_admin_notification(context.bot,
                    f"Забанен по pHash-аватару: @{user.username or user.first_name}")
                return
    except Exception:
        pass

    # 2) Текстовый спам-фильтр (как было)
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
    if "💋" in clean_name:
        ban = True
    for substr in cfg.get("BANNED_NAME_SUBSTRINGS", []):
        if normalize_text(substr) in name_lower:
            ban = True; break
    if not ban:
        norm_name = lemmatize_text(name_lower)
        banned = [lemmatize_text(normalize_text(n)) for n in cfg.get("BANNED_FULL_NAMES",[])]
        if norm_name in banned:
            ban = True
    if not ban and user.username:
        for substr in cfg.get("BANNED_USERNAME_SUBSTRINGS", []):
            if normalize_text(substr) in normalize_text(user.username):
                ban = True; break
    if not ban and any(s in clean_name for s in cfg.get("BANNED_SYMBOLS", [])):
        ban = True
    if not ban:
        for w in cfg.get("BANNED_WORDS", []):
            if w.lower() in text.lower():
                ban = True; break
    if not ban:
        for ph in cfg.get("PERMANENT_BLOCK_PHRASES", []):
            if lemmatize_text(normalize_text(ph)) in proc_text:
                ban = True; break
    if not ban:
        for combo in cfg.get("COMBINED_BLOCKS", []):
            if all(lemmatize_text(normalize_text(w)) in proc_text for w in combo):
                ban = True; break

    if ban:
        try: await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except: pass
        try: await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
        except: pass
        await send_admin_notification(context.bot,
            f"Забанен: @{user.username or user.first_name}\nИмя: {clean_name}\nДата: {get_tyumen_time()}\nСообщение: {text}")

async def spamlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        return await update.message.reply_text("Нет доступа.")
    cfg = load_config()
    text = (
        "<b>BANNED_WORDS</b>:\n" + "\n".join(cfg.get("BANNED_WORDS", [])) + "\n\n"
        "<b>BANNED_FULL_NAMES</b>:\n" + "\n".join(cfg.get("BANNED_FULL_NAMES", [])) + "\n\n"
        "<b>BANNED_SYMBOLS</b>:\n" + " ".join(cfg.get("BANNED_SYMBOLS", [])) + "\n\n"
        "<b>BANNED_NAME_SUBSTRINGS</b>:\n" + "\n".join(cfg.get("BANNED_NAME_SUBSTRINGS", [])) + "\n\n"
        "<b>PERMANENT_BLOCK_PHRASES</b>:\n" + "\n".join(cfg.get("PERMANENT_BLOCK_PHRASES", [])) + "\n\n"
        "<b>COMBINED_BLOCKS</b>:\n" + "\n".join([', '.join(c) for c in cfg.get("COMBINED_BLOCKS", [])])
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# … остальной запуск без изменений …
