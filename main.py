import os
import asyncio
import re
import nest_asyncio
import time
import json
from datetime import datetime, timedelta
from aiohttp import web

# Убедитесь, что Python == 3.10
import pymorphy2
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()
morph = pymorphy2.MorphAnalyzer()

# 1. Загружаем конфигурацию из файла config.json
def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

BANNED_FULL_NAMES = config.get("BANNED_FULL_NAMES", [])
PERMANENT_BLOCK_PHRASES = config.get("PERMANENT_BLOCK_PHRASES", [])
COMBINED_BLOCKS = config.get("COMBINED_BLOCKS", [])
BANNED_SYMBOLS = config.get("BANNED_SYMBOLS", [])

ADMIN_CHAT_ID = 296920330  # Ваш Telegram ID (для уведомлений)

def get_tyumen_time():
    # Тюменское время (UTC+5)
    return (datetime.utcnow() + timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S')

def get_chat_link(chat):
    if chat.username:
        return f"https://t.me/{chat.username}"
    elif chat.title:
        return f"https://t.me/{chat.title.replace(' ', '')}"
    else:
        return f"Chat ID: {chat.id}"

# 2. Нормализация (замена латинских символов на кириллические)
def normalize_text(text: str) -> str:
    mapping = {
        'a': 'а',
        'c': 'с',
        'e': 'е',
        'o': 'о',
        'p': 'р',
        'y': 'у',
        'x': 'х',
        '3': 'з',
        '0': 'о'
    }
    return ''.join(mapping.get(ch, ch) for ch in text.lower())

# 3. Лемматизация с помощью pymorphy2
def lemmatize_text(text: str) -> str:
    words = text.split()
    lemmatized_words = [morph.parse(word)[0].normal_form for word in words]
    return ' '.join(lemmatized_words)

async def send_admin_notification(bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Error sending admin notification:", e)

# Если не используются — оставляем пустыми
SPAM_WORDS = []
SPAM_PHRASES = []

async def restrict_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg and msg.new_chat_members:
        chat_link = get_chat_link(msg.chat)
        for member in msg.new_chat_members:
            until_date = int(time.time()) + 300  # Ограничение на 300 секунд для новых участников
            try:
                await context.bot.restrict_chat_member(
                    chat_id=msg.chat.id,
                    user_id=member.id,
                    permissions=ChatPermissions(
                        can_send_messages=False,
                        can_send_media_messages=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False
                    ),
                    until_date=until_date
                )
                print(f"New member {member.id} restricted for 300 seconds in chat {msg.chat.id} ({chat_link}).")
            except Exception as e:
                print("Error restricting new member:", e)
        # Удаляем уведомление о вступлении
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
            print("Deleted join notification message.")
        except Exception as e:
            print("Error deleting join notification message:", e)

async def delete_left_member_notification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg and msg.left_chat_member:
        try:
            await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
            print(f"Deleted left member notification in chat {msg.chat.id}.")
        except Exception as e:
            print("Error deleting left member notification:", e)

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or update.channel_post
    if msg and msg.text:
        text = msg.text
        # Лемматизируем + нормализуем (сначала нормализуем, потом лемматизируем)
        processed_text = lemmatize_text(normalize_text(text))
        print("Processed message:", processed_text)
        permanent_ban = False
        user = msg.from_user

        # Формируем полное имя ("first_name | last_name")
        full_name = user.first_name if user.first_name else ""
        if user.last_name:
            full_name += " | " + user.last_name

        # 1) Проверка по запрещённым полным именам
        normalized_name = lemmatize_text(normalize_text(full_name))
        banned_names = [lemmatize_text(normalize_text(n)) for n in BANNED_FULL_NAMES]
        if normalized_name in banned_names:
            print(f"Banned full name detected: {full_name}")
            permanent_ban = True

        # 2) Проверка по запрещённым символам в имени
        if any(symbol in full_name for symbol in BANNED_SYMBOLS):
            print(f"Banned symbol detected in full name: {full_name}")
            permanent_ban = True

        # 3) Проверка PERMANENT_BLOCK_PHRASES
        if not permanent_ban:
            for phrase in PERMANENT_BLOCK_PHRASES:
                normalized_phrase = lemmatize_text(normalize_text(phrase))
                if normalized_phrase in processed_text:
                    permanent_ban = True
                    break

        # 4) Проверка COMBINED_BLOCKS
        if not permanent_ban:
            for combo in COMBINED_BLOCKS:
                # Все слова combo должны присутствовать в processed_text
                if all(lemmatize_text(normalize_text(w)) in processed_text for w in combo):
                    permanent_ban = True
                    break

        # 5) Дополнительная проверка SPAM_WORDS и SPAM_PHRASES (если заданы)
        if not permanent_ban:
            for word in SPAM_WORDS:
                w = lemmatize_text(normalize_text(word))
                pattern = r'\b' + re.escape(w) + r'\b'
                if re.search(pattern, processed_text):
                    permanent_ban = True
                    break
            if not permanent_ban:
                for phrase in SPAM_PHRASES:
                    ph = lemmatize_text(normalize_text(phrase))
                    if ph in processed_text:
                        permanent_ban = True
                        break

        # Если хотя бы одно условие сработало
        if permanent_ban:
            chat_link = get_chat_link(msg.chat)
            block_time = get_tyumen_time()
            username = f"@{user.username}" if user.username else (user.first_name or str(user.id))
            notif = (
                f"Никнейм: {username}\n"
                f"Дата блокировки: {block_time}\n"
                f"Название канала: {chat_link}\n"
                f"Сообщение: {msg.text}"
            )
            print(notif)
            try:
                await context.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
                print("Offending message deleted.")
            except Exception as e:
                print("Error deleting offending message:", e)
            try:
                await context.bot.ban_chat_member(chat_id=msg.chat.id, user_id=user.id)
                print("User banned permanently.")
            except Exception as e:
                print("Error banning user:", e)
            await send_admin_notification(context.bot, notif)

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения")
    
    # Создаем приложение бота
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    # Регистрируем обработчики
    app_bot.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, restrict_new_member))
    app_bot.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, delete_left_member_notification))
    app_bot.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    
    await app_bot.initialize()
    
    webhook_url = "https://spampython-bot-py.onrender.com/webhook"
    await app_bot.bot.set_webhook(webhook_url)
    
    aio_app = web.Application()
    
    async def health(request):
        return web.Response(text="OK")
    aio_app.router.add_get("/", health)
    
    async def handle_webhook(request):
        data = await request.json()
        update = Update.de_json(data, app_bot.bot)
        await app_bot.process_update(update)
        return web.Response(text="OK")
    
    aio_app.router.add_post("/webhook", handle_webhook)
    
    return aio_app, port

async def main():
    aio_app, port = await init_app()
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Server running on port {port}")
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
else:
    loop = asyncio.get_event_loop()
    app, _ = loop.run_until_complete(init_app())
