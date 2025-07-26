#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

# --- СПАМ И ФИЛЬТРАЦИЯ ПО АВАТАРУ И ТЕКСТУ ---
async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return

    user = msg.from_user
    cfg = load_config()

    # 0) NSFW-фильтр аватара через DeepAI API
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count:
            f = await context.bot.get_file(photos.photos[0][-1].file_id)
            bio = BytesIO()
            await f.download_to_memory(out=bio)
            bio.seek(0)

            resp = requests.post(
                "https://api.deepai.org/api/nsfw-detector",
                files={'image': bio.getvalue()},
                headers={'api-key': os.getenv("DEEPAI_API_KEY")}
            )
            data = resp.json()
            nsfw_score = data.get("output", {}).get("nsfw_score", 0)
            if nsfw_score >= cfg.get("NSFW_THRESHOLD", 0.6):
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                await send_admin_notification(
                    context.bot,
                    f"Забанен по NSFW-аватару (score={nsfw_score:.2f}): @{user.username or user.first_name}"
                )
                return
    except Exception:
        pass

    # 1) pHash-фильтр аватара
    try:
        # img остался из предыдущего блока
        ph = imagehash.phash(Image.open(BytesIO(bio.getvalue())))
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
        await send_admin_notification(
            context.bot,
            f"Забанен: @{user.username or user.first_name}\n"
            f"Имя: {clean_name}\n"
            f"Дата: {get_tyumen_time()}\n"
            f"Сообщение: {text}"
        )

# --- /SPAMLIST и /ADDSPAM хендлеры без изменений ---
# …твой остальной код запуска…

async def init_app():
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    webhook = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("spamlist", spamlist))
    app.add_handler(MessageHandler(filters.ALL, delete_spam_message))

    await app.initialize()
    await app.bot.set_webhook(webhook)

    web_app = web.Application()
    web_app.router.add_get("/", lambda r: web.Response(text="OK"))
    web_app.router.add_post("/webhook", lambda r: web.Response(text="OK") if await app.process_update(Update.de_json(await r.json(), app.bot)) is None else None)
    return web_app, int(os.getenv("PORT", 8443))

if __name__ == "__main__":
    asyncio.run(init_app())
