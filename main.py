import os
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

async def on_startup(app):
    # Замените <your-app-name> на имя вашего приложения на Render
    webhook_url = f"https://<your-app-name>.onrender.com/webhook"
    await app.bot.set_webhook(webhook_url)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8443))
    app = ApplicationBuilder().token("7712516662:AAECNpqvIMy1OszmAAQxrBEuHYhQc52v4kQ").build()
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, delete_trading_message))
    
    app.run_webhook(listen="0.0.0.0", port=port, url_path="webhook", on_startup=on_startup)
