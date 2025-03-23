import os
import asyncio
import nest_asyncio
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

nest_asyncio.apply()

TRADING_WORD = "трейдинг"

async def delete_trading_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        if TRADING_WORD in update.message.text.lower():
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )

async def init_app():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения")
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, delete_trading_message))
    
    # Устанавливаем webhook
    webhook_url = "https://spampython-bot-py.onrender.com/webhook"
    await app_bot.bot.set_webhook(webhook_url)
    
    # Создаем объект aiohttp приложения
    aio_app = web.Application()
    
    # Добавляем endpoint для health check
    async def health(request):
        return web.Response(text="OK")
    aio_app.router.add_get("/", health)
    
    # Добавляем endpoint для webhook (здесь используем встроенный обработчик бота)
    aio_app.router.add_post("/webhook", app_bot.create_webhook_handler())
    
    return aio_app, port

async def main():
    aio_app, port = await init_app()
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Server running on port {port}")
    # Запускаем бесконечный цикл, чтобы приложение не завершилось
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
