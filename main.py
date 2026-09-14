import asyncio
import logging
import signal

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from handlers import cancel, forward_to_group, forward_to_user, setwelcome, start, welcome
from settings import PERSONAL_ACCOUNT_CHAT_ID, TELEGRAM_SUPPORT_CHAT_ID, TELEGRAM_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

stop_event = asyncio.Event()


async def main() -> None:
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    support_chats = list(
        {chat_id for chat_id in (TELEGRAM_SUPPORT_CHAT_ID, PERSONAL_ACCOUNT_CHAT_ID) if chat_id}
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("welcome", welcome))
    application.add_handler(CommandHandler("setwelcome", setwelcome))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND
            & filters.ChatType.PRIVATE
            & ~filters.Chat(chat_id=support_chats),
            forward_to_group,
        )
    )
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & filters.Chat(chat_id=support_chats) & filters.REPLY,
            forward_to_user,
        )
    )
    logging.info("Handlers registered for support chats %s", support_chats)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logging.info("Bot started")
    await stop_event.wait()
    logging.info("Stopping")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
