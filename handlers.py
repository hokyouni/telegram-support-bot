import json
import logging
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from settings import (
    ADMIN_USER_IDS,
    FORWARD_MODE,
    PERSONAL_ACCOUNT_CHAT_ID,
    TELEGRAM_SUPPORT_CHAT_ID,
    WELCOME_MESSAGE,
)

MAP_PATH = Path("/data/forward_map.json")
CONFIG_PATH = Path("/data/config.json")
ADMIN_STATUSES = {"creator", "administrator"}


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


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("failed to read config")
        return {}


def _save_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def get_welcome_message() -> str:
    stored = _load_config().get("welcome_message")
    if isinstance(stored, str) and stored.strip():
        return stored
    return WELCOME_MESSAGE


def set_welcome_message(text: str) -> None:
    cfg = _load_config()
    cfg["welcome_message"] = text.strip()
    _save_config(cfg)


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    if user.id in ADMIN_USER_IDS:
        return True
    try:
        member = await context.bot.get_chat_member(TELEGRAM_SUPPORT_CHAT_ID, user.id)
        return member.status in ADMIN_STATUSES
    except TelegramError as exc:
        logging.warning("admin check failed for %s: %s", user.id, exc)
        return False


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await _is_admin(update, context):
        return True
    if update.message:
        await update.message.reply_text("只有支持群管理员可以修改欢迎语。")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(get_welcome_message())


async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not await _require_admin(update, context):
        return
    await update.message.reply_text(f"当前欢迎语：\n{get_welcome_message()}")


async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not await _require_admin(update, context):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("请私聊机器人使用 /setwelcome，避免把新文案发到群里。")
        return

    replied = update.message.reply_to_message
    if replied and (replied.text or replied.caption):
        set_welcome_message(replied.text or replied.caption)
        context.user_data.pop("awaiting_welcome", None)
        await update.message.reply_text("欢迎语已更新。")
        return

    payload = update.message.text.split(maxsplit=1) if update.message.text else []
    if len(payload) > 1 and payload[1].strip():
        set_welcome_message(payload[1])
        context.user_data.pop("awaiting_welcome", None)
        await update.message.reply_text("欢迎语已更新。")
        return

    context.user_data["awaiting_welcome"] = True
    await update.message.reply_text("请发送新的欢迎语。发送 /cancel 取消。")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if context.user_data.pop("awaiting_welcome", None):
        await update.message.reply_text("已取消。")
        return
    await update.message.reply_text("当前没有进行中的操作。")


async def forward_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if context.user_data.get("awaiting_welcome"):
        if await _is_admin(update, context) and (update.message.text or update.message.caption):
            set_welcome_message(update.message.text or update.message.caption)
            context.user_data.pop("awaiting_welcome", None)
            await update.message.reply_text("欢迎语已更新。")
            return
        context.user_data.pop("awaiting_welcome", None)
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
