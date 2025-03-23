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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8443))
    app = ApplicationBuilder().token("7712516662:AAECNpqvIMy1OszmAAQxrBEuHYhQc52v4kQ").build()
    
    # Настройка webhook (укажи URL своего приложения)
    webhook_url = f"https://<your-app-name>.onrender.com/webhook"
    app.bot.set_webhook(webhook_url)
    
    # Запуск веб-сервера на нужном порту
    app.run_webhook(listen="0.0.0.0", port=port, url_path="webhook")
