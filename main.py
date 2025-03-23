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

# Список спам-фраз (последовательности из 2 и более слов)
SPAM_PHRASES = ["курсы по торговле", "курсы по трейдингу"]

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Обрабатываем как обычные сообщения, так и сообщения из каналов
    msg = update.message or update.channel_post
    if msg and msg.text:
        text = msg.text.lower()
        print("Received message:", text)
        spam_found = False

        # Проверяем отдельные слова (с учетом границ слова)
        for word in SPAM_WORDS:
            if re.search(r'\b' + re.escape(word) + r'\b', text):
                spam_found = True
                break

        # Если не найдено по отдельным словам, проверяем спам-фразы (подстрока)
        if not spam_found:
            for phrase in SPAM_PHRASES:
                if phrase in text:
                    spam_found = True
                    break

        if spam_found:
            print("Spam detected in message id:", msg.message_id)
            try:
                await context.bot.delete_message(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id
                )
                print("Message deleted.")
            except Exception as e:
                print("Error deleting message:", e)
            
            # Если сообщение в группе, ограничиваем права пользователя (блокировка на 1 час)
            if msg.chat.type in ["group", "supergroup"]:
                if msg.from_user:
                    try:
                        until_date = int(time.time()) + 3600  # блокировка на 1 час
                        await context.bot.restrict_chat_member(
                            chat_id=msg.chat.id,
                            user_id=msg.from_user.id,
                            permissions=ChatPermissions(can_send_messages=False),
                            until_date=until_date
                        )
                        print("User restricted until", until_date)
                    except Exception as e:
                        print("Error restricting user:", e)

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения")
    
    # Создаем приложение бота и добавляем обработчик для всех обновлений
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    await app_bot.initialize()
    
    # Устанавливаем webhook (убеди­сь, что URL соответствует домену твоего приложения на Render)
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
