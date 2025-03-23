import os
import asyncio
import re
import nest_asyncio
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()

# Список спам-слов
SPAM_WORDS = ["трейдинг", "трейдер", "криптовалюта", "крипто"]

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or update.channel_post
    if msg and msg.text:
        text = msg.text.lower()
        print("Received message:", text)
        pattern = r'\b(' + '|'.join(SPAM_WORDS) + r')\b'
        if re.search(pattern, text):
            print("Spam word detected. Deleting message, id:", msg.message_id)
            try:
                await context.bot.delete_message(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id
                )
            except Exception as e:
                print("Error deleting message:", e)

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения")
    
    # Создаем приложение бота и добавляем обработчик
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(MessageHandler(filters.ALL, delete_spam_message))
    
    # Инициализируем приложение (важно!)
    await app_bot.initialize()
    
    # Устанавливаем webhook (убеди­сь, что URL корректный)
    webhook_url = "https://spampython-bot-py.onrender.com/webhook"
    await app_bot.bot.set_webhook(webhook_url)
    
    # Создаем aiohttp-приложение для health check и обработки вебхука
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
    # Держим сервер запущенным
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
if __name__ == '__main__':
    asyncio.run(main())
