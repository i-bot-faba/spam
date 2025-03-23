import os
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

TRADING_WORD = "трейдинг"

async def delete_trading_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        if TRADING_WORD in update.message.text.lower():
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )

async def main():
    port = int(os.environ.get("PORT", 8443))
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения")
    
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, delete_trading_message))
    
    # Укажи правильный URL: замените <your-app-name> на имя твоего приложения на Render
    webhook_url = f"https://<your-app-name>.onrender.com/webhook"
    await app.bot.set_webhook(webhook_url)
    
    await app.run_webhook(listen="0.0.0.0", port=port, url_path="webhook")

if __name__ == '__main__':
    asyncio.run(main())
