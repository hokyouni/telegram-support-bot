import os

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is required")

TELEGRAM_SUPPORT_CHAT_ID = os.getenv("TELEGRAM_SUPPORT_CHAT_ID")
if (
    TELEGRAM_SUPPORT_CHAT_ID is None
    or not str(TELEGRAM_SUPPORT_CHAT_ID).lstrip("-").isdigit()
):
    raise RuntimeError("TELEGRAM_SUPPORT_CHAT_ID must be a numeric chat id")
TELEGRAM_SUPPORT_CHAT_ID = int(TELEGRAM_SUPPORT_CHAT_ID)

WELCOME_MESSAGE = os.getenv(
    "WELCOME_MESSAGE", "你好，请直接发送消息，我们会尽快回复。"
)
FORWARD_MODE = os.getenv("FORWARD_MODE", "support_chat")

ADMIN_USER_IDS = set()
for _part in os.getenv("ADMIN_USER_IDS", "").split(","):
    _part = _part.strip()
    if _part.lstrip("-").isdigit():
        ADMIN_USER_IDS.add(int(_part))

PERSONAL_ACCOUNT_CHAT_ID = os.getenv("PERSONAL_ACCOUNT_CHAT_ID")
if PERSONAL_ACCOUNT_CHAT_ID and str(PERSONAL_ACCOUNT_CHAT_ID).lstrip("-").isdigit():
    PERSONAL_ACCOUNT_CHAT_ID = int(PERSONAL_ACCOUNT_CHAT_ID)
else:
    PERSONAL_ACCOUNT_CHAT_ID = TELEGRAM_SUPPORT_CHAT_ID
