import json
import logging
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from settings import (
    FORWARD_MODE,
    PERSONAL_ACCOUNT_CHAT_ID,
    TELEGRAM_SUPPORT_CHAT_ID,
    WELCOME_MESSAGE,
)

MAP_PATH = Path("/data/forward_map.json")


def _target_chat() -> int:
    if FORWARD_MODE == "personal_account":
        return PERSONAL_ACCOUNT_CHAT_ID
    return TELEGRAM_SUPPORT_CHAT_ID


def _load_map() -> dict:
    if not MAP_PATH.exists():
        return {}
    try:
        return json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("failed to read forward map")
        return {}


def _save_map(data: dict) -> None:
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAP_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MAP_PATH)


def _remember(context: ContextTypes.DEFAULT_TYPE, group_message_id: int, user_id: int) -> None:
    fwd = context.bot_data.setdefault("fwd", {})
    fwd[str(group_message_id)] = user_id
    disk = _load_map()
    disk[str(group_message_id)] = user_id
    _save_map(disk)


def _lookup(context: ContextTypes.DEFAULT_TYPE, group_message_id: int):
    fwd = context.bot_data.get("fwd") or {}
    key = str(group_message_id)
    if key in fwd:
        return fwd[key]
    return _load_map().get(key)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    name = update.effective_user.first_name if update.effective_user else ""
    await update.message.reply_text(f"{WELCOME_MESSAGE} {name}".strip())


async def forward_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    target = _target_chat()
    user = update.effective_user
    try:
        forwarded = await update.message.forward(target)
        _remember(context, forwarded.message_id, user.id)
        return
    except TelegramError as exc:
        logging.warning("forward failed, falling back to copy: %s", exc)

    try:
        copied = await update.message.copy(target)
        _remember(context, copied.message_id, user.id)
        label = f"来自 {user.full_name} id={user.id}"
        if user.username:
            label += f" @{user.username}"
        await context.bot.send_message(
            chat_id=target,
            text=label,
            reply_to_message_id=copied.message_id,
        )
    except TelegramError as exc:
        logging.error("copy to support chat failed: %s", exc)
        await update.message.reply_text("转发失败，请稍后再试。")


async def forward_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.reply_to_message:
        return
    replied = update.message.reply_to_message
    user_id = _lookup(context, replied.message_id)
    if user_id is None and replied.reply_to_message:
        user_id = _lookup(context, replied.reply_to_message.message_id)
    if user_id is None:
        await update.message.reply_text("找不到对应的用户，请回复机器人转发过来的那条消息。")
        return
    try:
        await context.bot.copy_message(
            chat_id=int(user_id),
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
    except TelegramError as exc:
        logging.error("send back to user failed: %s", exc)
        await update.message.reply_text(f"发送失败: {exc}")
