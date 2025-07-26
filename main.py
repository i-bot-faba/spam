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

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
