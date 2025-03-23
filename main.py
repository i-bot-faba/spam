import os
import asyncio
import re
import nest_asyncio
import time
from aiohttp import web
from telegram import Update, ChatPermissions
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()

# Список спам-слов (отдельные слова)
SPAM_WORDS = ["трейдинг", "трейдер", "криптовалюта", "крипто"]

# Список спам-фраз (последовательности слов, если нужно)
SPAM_PHRASES = ["курсы по трейдингу", "трейдинг криптовалюта"]

async def restrict_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg and msg.new_chat_members:
        for member in msg.new_chat_members:
            until_date = int(time.time()) + 300  # ограничение на 5 минут
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
                print(f"Restricted new member {member.id} for 10 minutes.")
            except Exception as e:
                print("Error restricting new member:", e)

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Обрабатываем сообщения (обычные или из каналов)
    msg = update.message or update.channel_post
    if msg and msg.text:
        text = msg.text.lower()
        print("Received message:", text)
        spam_found = False

        # Проверяем наличие спам-слов
        for word in SPAM_WORDS:
            if re.search(r'\b' + re.escape(word) + r'\b', text):
                spam_found = True
                break

        # Если по отдельным словам не найдено, проверяем спам-фразы
        if not spam_found:
            for phrase in SPAM_PHRASES:
                if phrase in text:
                    spam_found = True
                    break

        if spam_found:
            print("Spam detected in message id:", msg.message_id)
            try:
                # Баним пользователя навсегда
                await context.bot.ban_chat_member(
                    chat_id=msg.chat.id,
                    user_id=msg.from_user.id
                )
                print("User banned permanently.")
            except Exception as e:
                print("Error banning user:", e)

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения")
    
    # Создаем приложение бота
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    # Регистрируем сначала обработчик для новых участников
    app_bot.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, restrict_new_member))
    # Затем общий обработчик для остальных сообщений
    app_bot.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    
    # Инициализируем приложение
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
