import os
import asyncio
import re
import nest_asyncio
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()

# Список спам-слов (по одному слову)
SPAM_WORDS = ["трейдинг", "трейдер", "криптовалюта", "крипто"]

async def delete_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        text = update.message.text.lower()
        # Ищем любое из спам-слов по границам слова
        pattern = r'\b(' + '|'.join(SPAM_WORDS) + r')\b'
        if re.search(pattern, text):
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения")
    
    # Создаем приложение бота и добавляем обработчик сообщений
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, delete_spam_message))
    
    # Устанавливаем webhook у Telegram (замени URL на актуальный домен твоего приложения на Render)
    webhook_url = "https://spampython-bot-py.onrender.com/webhook"
    await app_bot.bot.set_webhook(webhook_url)
    
    # Создаем aiohttp-приложение для обработки запросов от Telegram и health check
    aio_app = web.Application()
    
    # Endpoint для health check (Render проверяет GET-запрос на "/")
    async def health(request):
        return web.Response(text="OK")
    aio_app.router.add_get("/", health)
    
    # Endpoint для обработки вебхука (POST /webhook)
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
