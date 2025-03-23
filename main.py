import os
import asyncio
import re
import nest_asyncio
import time
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()

ADMIN_CHAT_ID = 296920330  # Твой числовой ID

def get_local_time():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())

def get_tyumen_time():
    # Предположим, что Тюменское время = UTC+5
    return (datetime.utcnow() + timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S')

def get_chat_link(chat):
    if chat.username:
        return f"https://t.me/{chat.username}"
    elif chat.title:
        return chat.title
    else:
        return f"Chat ID: {chat.id}"

async def send_admin_notification(bot, text: str) -> None:
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print("Error sending admin notification:", e)

# Оригинальные спам-слова и спам-фразы (оставляем пустыми, если не используются)
SPAM_WORDS = []      
SPAM_PHRASES = []    

# Фразы для постоянной блокировки (если встречается хотя бы одна, блокируем навсегда)
PERMANENT_BLOCK_PHRASES = [
    "хватит жить на мели!",
    "начни зарабатывать",
    "хватит сидеть без денег!",
    "давай заработаем",
    "от 8000р в день",
    "от 9000р в день",
    "от 10000р в день",
    "от 11000р в день",
    "от 12000р в день",
    "от 13000р в день",
    "от 14000р в день",
    "приобрёл полезные курсы",
    "курсы по торговле",
    "курсы по трейдингу",
    "дочитываю книгу",
    "сорос",
    "курсы по инвестициям",
    "безвозвратно поделиться"
]

# Комбинации слов для блокировки (если в сообщении присутствуют все слова из комбинации)
COMBINED_BLOCKS = [
    ["трейдинг", "инвестиции", "криптовалюты"],
    ["трейдинг", "недвижимость"],
    ["трейдинг", "инвестиции"],
    ["трейдинг", "торговля"]
]

async def restrict_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg and msg.new_chat_members:
        print("New members joined:", [member.id for member in msg.new_chat_members])
        chat_link = get_chat_link(msg.chat)
        for member in msg.new_chat_members:
            until_date = int(time.time()) + 30  # ограничение на 30 секунд
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
                username = f"@{member.username}" if member.username else (member.first_name or str(member.id))
                notif = (f"[{get_local_time()}] New member {member.id} ({username}) restricted for 30 seconds "
                         f"in chat {msg.chat.id} ({chat_link}).")
                print(notif)
                await send_admin_notification(context.bot, notif)
            except Exception as e:
                print("Error restricting new member:", e)
        # Удаляем уведомление о вступлении
        try:
            await context.bot.delete_message(
                chat_id=msg.chat.id,
                message_id=msg.message_id
            )
            print("Deleted join notification message.")
        except Exception as e:
            print("Error deleting join notification message:", e)

async def delete_left_member_notification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg and msg.left_chat_member:
        chat_link = get_chat_link(msg.chat)
        try:
            await context.bot.delete_message(
                chat_id=msg.chat.id,
                message_id=msg.message_id
            )
            notif = f"[{get_local_time()}] Left member notification deleted in chat {msg.chat.id} ({chat_link})."
            print(notif)
            await send_admin_notification(context.bot, notif)
        except Exception as e:
            print("Error deleting left member notification:", e)

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or update.channel_post
    if msg and msg.text:
        text = msg.text.lower()
        print("Received message:", text)
        permanent_ban = False

        # Проверяем наличие фраз для постоянной блокировки
        for phrase in PERMANENT_BLOCK_PHRASES:
            if phrase in text:
                print(f"Permanent block phrase detected: {phrase}")
                permanent_ban = True
                break

        # Проверяем комбинации слов (если все слова из комбинации присутствуют)
        if not permanent_ban:
            for combo in COMBINED_BLOCKS:
                if all(word in text for word in combo):
                    print(f"Combined block detected: {combo}")
                    permanent_ban = True
                    break

        # Если не сработали выше условия, проверяем оригинальные спам-слова/фразы (пустые сейчас)
        if not permanent_ban:
            for word in SPAM_WORDS:
                if re.search(r'\b' + re.escape(word) + r'\b', text):
                    print(f"Spam word detected: {word}")
                    permanent_ban = True
                    break
            if not permanent_ban:
                for phrase in SPAM_PHRASES:
                    if phrase in text:
                        print(f"Spam phrase detected: {phrase}")
                        permanent_ban = True
                        break

        if permanent_ban:
            user = msg.from_user
            username = f"@{user.username}" if user.username else (user.first_name or str(user.id))
            chat_link = get_chat_link(msg.chat)
            block_date = get_local_time()
            tyumen_time = get_tyumen_time()
            notif = (f"Никнейм: {username}\n"
                     f"Дата блокировки: {block_date}\n"
                     f"Время Тюменское: {tyumen_time}\n"
                     f"Название канала: {chat_link}\n"
                     f"Сообщение: {msg.text}")
            print(notif)
            # Сначала удаляем сообщение с нарушением
            try:
                await context.bot.delete_message(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id
                )
                print("Offending message deleted.")
            except Exception as e:
                print("Error deleting offending message:", e)
            # Блокируем пользователя навсегда
            try:
                await context.bot.ban_chat_member(
                    chat_id=msg.chat.id,
                    user_id=user.id
                )
                print("User banned permanently.")
            except Exception as e:
                print("Error banning user:", e)
            # Отправляем уведомление админу
            await send_admin_notification(context.bot, notif)

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения")
    
    # Создаем приложение бота
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    # Регистрируем обработчик для новых участников (ограничение на 30 секунд)
    app_bot.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, restrict_new_member))
    # Регистрируем обработчик для уведомлений об уходе (удаляем уведомления)
    app_bot.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, delete_left_member_notification))
    # Регистрируем общий обработчик для сообщений (проверка спама и блокировка)
    app_bot.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    
    await app_bot.initialize()
    
    # Устанавливаем webhook (убеди­сь, что URL корректный)
    webhook_url = "https://spampython-bot-py.onrender.com/webhook"
    await app_bot.bot.set_webhook(webhook_url)
    
    # Создаем aiohttp-приложение для health check и обработки webhook-обновлений
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
