import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence, TypeVar

from telegram import (
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    MessageEntity,
    ReactionType,
    ReactionTypeEmoji,
    ReplyParameters,
    Update,
)
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ContextTypes

from settings import (
    ADMIN_USER_IDS,
    FORWARD_MODE,
    PERSONAL_ACCOUNT_CHAT_ID,
    TELEGRAM_SUPPORT_CHAT_ID,
    WELCOME_MESSAGE,
)

MAP_PATH = Path("/data/forward_map.json")
LINKS_PATH = Path("/data/message_links.json")
CONFIG_PATH = Path("/data/config.json")
ADMIN_STATUSES = {"creator", "administrator"}
MAX_LINKS = 5000
MAX_TEXT_LENGTH = 4096

MAX_RETRY_ATTEMPTS = 4
MAX_TIMEOUT_ATTEMPTS = 2
RETRY_BASE_DELAY = 0.8
MAX_RETRY_AFTER = 15.0

T = TypeVar("T")
_STATE_LOCK = asyncio.Lock()


def support_chat_ids() -> set[int]:
    return {
        chat_id
        for chat_id in (TELEGRAM_SUPPORT_CHAT_ID, PERSONAL_ACCOUNT_CHAT_ID)
        if chat_id
    }


def _target_chat() -> int:
    if FORWARD_MODE == "personal_account":
        return PERSONAL_ACCOUNT_CHAT_ID
    return TELEGRAM_SUPPORT_CHAT_ID


def _user_label(user) -> str:
    label = f"{user.full_name} id={user.id}"
    if user.username:
        label += f" @{user.username}"
    return label


async def _call_with_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    op_name: str,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
    max_timeout_attempts: int = MAX_TIMEOUT_ATTEMPTS,
) -> T:
    timeout_attempts = 0
    for attempt in range(1, max_attempts + 1):
        try:
            return await factory()
        except RetryAfter as exc:
            if attempt >= max_attempts:
                raise
            delay = min(float(exc.retry_after), MAX_RETRY_AFTER) + 0.2
            logging.warning(
                "%s hit flood control (attempt %d/%d), retrying in %.1fs",
                op_name,
                attempt,
                max_attempts,
                delay,
            )
            await asyncio.sleep(delay)
        except BadRequest:
            raise
        except TimedOut as exc:
            timeout_attempts += 1
            if attempt >= max_attempts or timeout_attempts >= max_timeout_attempts:
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logging.warning(
                "%s timed out (attempt %d/%d): %s; retrying in %.1fs",
                op_name,
                attempt,
                max_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
        except NetworkError as exc:
            if attempt >= max_attempts:
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logging.warning(
                "%s network error (attempt %d/%d): %s; retrying in %.1fs",
                op_name,
                attempt,
                max_attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"{op_name} exhausted retries")


def _atomic_write_json(path: Path, data: Any, *, indent: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    tmp.replace(path)


def _load_map() -> dict[str, int]:
    if not MAP_PATH.exists():
        return {}
    try:
        data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("failed to read forward map")
        return {}


def _save_map(data: dict[str, int]) -> None:
    _atomic_write_json(MAP_PATH, data)


def _load_links() -> dict[str, dict[str, dict[str, Any]]]:
    empty: dict[str, dict[str, dict[str, Any]]] = {"group_to_user": {}, "user_to_group": {}}
    if not LINKS_PATH.exists():
        return empty
    try:
        data = json.loads(LINKS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        g2u = data.get("group_to_user")
        u2g = data.get("user_to_group")
        return {
            "group_to_user": g2u if isinstance(g2u, dict) else {},
            "user_to_group": u2g if isinstance(u2g, dict) else {},
        }
    except Exception:
        logging.exception("failed to read message links")
        return empty


def _trim_dict(mapping: dict[str, Any], limit: int) -> None:
    excess = len(mapping) - limit
    if excess <= 0:
        return
    for old_key in list(mapping.keys())[:excess]:
        mapping.pop(old_key, None)


def _save_links(data: dict[str, dict[str, dict[str, Any]]]) -> None:
    _trim_dict(data["group_to_user"], MAX_LINKS)
    _trim_dict(data["user_to_group"], MAX_LINKS)
    _atomic_write_json(LINKS_PATH, data)


def _group_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _user_key(user_id: int, message_id: int) -> str:
    return f"{user_id}:{message_id}"


def _get_links_cache(context: ContextTypes.DEFAULT_TYPE) -> dict[str, dict[str, dict[str, Any]]]:
    links = context.bot_data.get("links")
    if links is None:
        links = _load_links()
        context.bot_data["links"] = links
    return links


async def _remember_link(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    group_chat_id: int,
    group_message_id: int,
    user_id: int,
    user_message_id: int,
    direction: str,
    update_user_index: bool = True,
    remember_forward_map: bool = True,
) -> None:
    async with _STATE_LOCK:
        if remember_forward_map:
            fwd = context.bot_data.setdefault("fwd", {})
            fwd[str(group_message_id)] = user_id
            disk = _load_map()
            disk[str(group_message_id)] = user_id
            _save_map(disk)

        links = _get_links_cache(context)
        gkey = _group_key(group_chat_id, group_message_id)
        ukey = _user_key(user_id, user_message_id)

        # Refresh insertion order on update so LRU trimming keeps active threads
        links["group_to_user"].pop(gkey, None)
        links["group_to_user"][gkey] = {
            "user_id": user_id,
            "user_msg_id": user_message_id,
            "direction": direction,
        }
        if update_user_index:
            links["user_to_group"].pop(ukey, None)
            links["user_to_group"][ukey] = {
                "chat_id": group_chat_id,
                "group_msg_id": group_message_id,
                "direction": direction,
            }
        _save_links(links)


def _lookup(context: ContextTypes.DEFAULT_TYPE, group_message_id: int):
    fwd = context.bot_data.get("fwd") or {}
    key = str(group_message_id)
    if key in fwd:
        return fwd[key]
    return _load_map().get(key)


def _lookup_group_link(
    context: ContextTypes.DEFAULT_TYPE, group_chat_id: int, group_message_id: int
) -> Optional[dict[str, Any]]:
    links = _get_links_cache(context)
    return links["group_to_user"].get(_group_key(group_chat_id, group_message_id))


def _lookup_user_link(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, user_message_id: int
) -> Optional[dict[str, Any]]:
    links = _get_links_cache(context)
    return links["user_to_group"].get(_user_key(user_id, user_message_id))


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
    _atomic_write_json(CONFIG_PATH, data, indent=2)


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
        member = await _call_with_retry(
            lambda: context.bot.get_chat_member(TELEGRAM_SUPPORT_CHAT_ID, user.id),
            op_name="get_chat_member",
        )
        return member.status in ADMIN_STATUSES
    except TelegramError as exc:
        logging.warning("admin check failed for %s: %s", user.id, exc)
        return False


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await _is_admin(update, context):
        return True
    if update.message:
        await _call_with_retry(
            lambda: update.message.reply_text("只有支持群管理员可以修改欢迎语。"),
            op_name="reply_not_admin",
        )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await _call_with_retry(
        lambda: update.message.reply_text(get_welcome_message()),
        op_name="reply_start",
    )


async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not await _require_admin(update, context):
        return
    await _call_with_retry(
        lambda: update.message.reply_text(f"当前欢迎语：\n{get_welcome_message()}"),
        op_name="reply_welcome",
    )


async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not await _require_admin(update, context):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await _call_with_retry(
            lambda: update.message.reply_text("请私聊机器人使用 /setwelcome，避免把新文案发到群里。"),
            op_name="reply_setwelcome_private",
        )
        return

    replied = update.message.reply_to_message
    if replied and (replied.text or replied.caption):
        set_welcome_message(replied.text or replied.caption)
        context.user_data.pop("awaiting_welcome", None)
        await _call_with_retry(
            lambda: update.message.reply_text("欢迎语已更新。"),
            op_name="reply_setwelcome_ok",
        )
        return

    payload = update.message.text.split(maxsplit=1) if update.message.text else []
    if len(payload) > 1 and payload[1].strip():
        set_welcome_message(payload[1])
        context.user_data.pop("awaiting_welcome", None)
        await _call_with_retry(
            lambda: update.message.reply_text("欢迎语已更新。"),
            op_name="reply_setwelcome_ok",
        )
        return

    context.user_data["awaiting_welcome"] = True
    await _call_with_retry(
        lambda: update.message.reply_text("请发送新的欢迎语。发送 /cancel 取消。"),
        op_name="reply_setwelcome_prompt",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if context.user_data.pop("awaiting_welcome", None):
        await _call_with_retry(
            lambda: update.message.reply_text("已取消。"),
            op_name="reply_cancel_ok",
        )
        return
    await _call_with_retry(
        lambda: update.message.reply_text("当前没有进行中的操作。"),
        op_name="reply_cancel_noop",
    )


async def forward_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if context.user_data.get("awaiting_welcome"):
        if await _is_admin(update, context) and (update.message.text or update.message.caption):
            set_welcome_message(update.message.text or update.message.caption)
            context.user_data.pop("awaiting_welcome", None)
            await _call_with_retry(
                lambda: update.message.reply_text("欢迎语已更新。"),
                op_name="reply_setwelcome_ok",
            )
            return
        context.user_data.pop("awaiting_welcome", None)

    msg = update.message
    target = _target_chat()
    user = update.effective_user

    try:
        forwarded = await _call_with_retry(
            lambda: msg.forward(target),
            op_name="forward_to_group",
        )
        await _remember_link(
            context,
            group_chat_id=target,
            group_message_id=forwarded.message_id,
            user_id=user.id,
            user_message_id=msg.message_id,
            direction="in",
        )
        logging.info(
            "forwarded user=%s msg=%s -> chat=%s msg=%s",
            user.id,
            msg.message_id,
            target,
            forwarded.message_id,
        )
        return
    except TelegramError as exc:
        logging.warning("forward failed for user=%s msg=%s, falling back to copy: %s", user.id, msg.message_id, exc)

    try:
        copied = await _call_with_retry(
            lambda: msg.copy(target),
            op_name="copy_to_group",
        )
        await _remember_link(
            context,
            group_chat_id=target,
            group_message_id=copied.message_id,
            user_id=user.id,
            user_message_id=msg.message_id,
            direction="in",
        )
        label = f"来自 {_user_label(user)}"
        try:
            label_msg = await _call_with_retry(
                lambda: context.bot.send_message(
                    chat_id=target,
                    text=label,
                    reply_parameters=ReplyParameters(
                        message_id=copied.message_id,
                        allow_sending_without_reply=True,
                    ),
                ),
                op_name="send_copy_label",
            )
            await _remember_link(
                context,
                group_chat_id=target,
                group_message_id=label_msg.message_id,
                user_id=user.id,
                user_message_id=msg.message_id,
                direction="in",
                update_user_index=False,
            )
        except TelegramError as exc:
            logging.warning("label send failed after copy succeeded: %s", exc)
        logging.info(
            "copied user=%s msg=%s -> chat=%s msg=%s",
            user.id,
            msg.message_id,
            target,
            copied.message_id,
        )
    except TelegramError as exc:
        logging.error("copy to support chat failed for user=%s msg=%s: %s", user.id, msg.message_id, exc)
        try:
            await _call_with_retry(
                lambda: msg.reply_text("转发失败，请稍后再试。"),
                op_name="reply_forward_failed",
            )
        except TelegramError:
            logging.exception("failed to notify user about forward failure")


async def forward_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.reply_to_message or not update.effective_chat:
        return
    msg = update.message
    chat_id = update.effective_chat.id
    replied = msg.reply_to_message

    user_id = _lookup(context, replied.message_id)
    if user_id is None:
        link = _lookup_group_link(context, chat_id, replied.message_id)
        if link:
            user_id = link["user_id"]
    if user_id is None and replied.reply_to_message:
        user_id = _lookup(context, replied.reply_to_message.message_id)
    if user_id is None:
        await _call_with_retry(
            lambda: msg.reply_text("找不到对应的用户，请回复机器人转发过来的那条消息。"),
            op_name="reply_user_not_found",
        )
        return

    try:
        sent = await _call_with_retry(
            lambda: context.bot.copy_message(
                chat_id=int(user_id),
                from_chat_id=chat_id,
                message_id=msg.message_id,
            ),
            op_name="copy_to_user",
        )
        await _remember_link(
            context,
            group_chat_id=chat_id,
            group_message_id=msg.message_id,
            user_id=int(user_id),
            user_message_id=sent.message_id,
            direction="out",
        )
        logging.info(
            "replied chat=%s msg=%s -> user=%s msg=%s",
            chat_id,
            msg.message_id,
            user_id,
            sent.message_id,
        )
    except TelegramError as exc:
        logging.error("send back to user=%s failed: %s", user_id, exc)
        try:
            await _call_with_retry(
                lambda: msg.reply_text(f"发送失败: {exc}"),
                op_name="reply_send_failed",
            )
        except TelegramError:
            logging.exception("failed to notify support chat about send failure")


def _shift_entities(
    entities: Optional[Sequence[MessageEntity]], prefix: str
) -> Optional[list[MessageEntity]]:
    if not entities:
        return None
    delta = len(prefix.encode("utf-16-le")) // 2
    shifted: list[MessageEntity] = []
    for ent in entities:
        data = ent.to_dict()
        data["offset"] = ent.offset + delta
        restored = MessageEntity.de_json(data, None)
        if restored is not None:
            shifted.append(restored)
    return shifted


async def edit_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message
    user = update.effective_user
    if not msg or not user:
        return

    link = _lookup_user_link(context, user.id, msg.message_id)
    target = link["chat_id"] if link else _target_chat()
    orig_group_msg_id = link["group_msg_id"] if link else None
    reply_params = (
        ReplyParameters(message_id=orig_group_msg_id, allow_sending_without_reply=True)
        if orig_group_msg_id is not None
        else None
    )

    try:
        if msg.text is not None:
            header = (
                "✏️ 用户编辑了消息：\n"
                if orig_group_msg_id is not None
                else f"✏️ 来自 {_user_label(user)} 编辑了消息：\n"
            )
            full_text = f"{header}{msg.text}"
            if len(full_text) <= MAX_TEXT_LENGTH:
                entities = _shift_entities(msg.entities, header)
                sent = await _call_with_retry(
                    lambda: context.bot.send_message(
                        chat_id=target,
                        text=full_text,
                        entities=entities,
                        reply_parameters=reply_params,
                    ),
                    op_name="send_edited_text_to_group",
                )
            else:
                notice = await _call_with_retry(
                    lambda: context.bot.send_message(
                        chat_id=target,
                        text=header.rstrip("\n"),
                        reply_parameters=reply_params,
                    ),
                    op_name="send_edit_header_to_group",
                )
                await _remember_link(
                    context,
                    group_chat_id=target,
                    group_message_id=notice.message_id,
                    user_id=user.id,
                    user_message_id=msg.message_id,
                    direction="in",
                    update_user_index=False,
                )
                sent = await _call_with_retry(
                    lambda: context.bot.send_message(
                        chat_id=target,
                        text=msg.text,
                        entities=msg.entities,
                        reply_parameters=ReplyParameters(
                            message_id=notice.message_id,
                            allow_sending_without_reply=True,
                        ),
                    ),
                    op_name="send_edited_long_text_to_group",
                )
            await _remember_link(
                context,
                group_chat_id=target,
                group_message_id=sent.message_id,
                user_id=user.id,
                user_message_id=msg.message_id,
                direction="in",
            )
            logging.info(
                "synced user edit user=%s msg=%s -> chat=%s msg=%s",
                user.id,
                msg.message_id,
                target,
                sent.message_id,
            )
            return

        # Media / caption edit: copy the updated message with reply context, then tag it.
        copied = await _call_with_retry(
            lambda: context.bot.copy_message(
                chat_id=target,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                reply_parameters=reply_params,
            ),
            op_name="copy_edited_media_to_group",
        )
        await _remember_link(
            context,
            group_chat_id=target,
            group_message_id=copied.message_id,
            user_id=user.id,
            user_message_id=msg.message_id,
            direction="in",
        )
        tag_text = (
            "✏️ 用户编辑了这条消息"
            if orig_group_msg_id is not None
            else f"✏️ 来自 {_user_label(user)} 编辑了这条消息"
        )
        try:
            tag_msg = await _call_with_retry(
                lambda: context.bot.send_message(
                    chat_id=target,
                    text=tag_text,
                    reply_parameters=ReplyParameters(
                        message_id=copied.message_id,
                        allow_sending_without_reply=True,
                    ),
                ),
                op_name="send_edited_media_tag",
            )
            await _remember_link(
                context,
                group_chat_id=target,
                group_message_id=tag_msg.message_id,
                user_id=user.id,
                user_message_id=msg.message_id,
                direction="in",
                update_user_index=False,
            )
        except TelegramError as exc:
            logging.warning("edit tag send failed after copy succeeded: %s", exc)
        logging.info(
            "synced user media edit user=%s msg=%s -> chat=%s msg=%s",
            user.id,
            msg.message_id,
            target,
            copied.message_id,
        )
    except TelegramError as exc:
        logging.error("sync user edit failed for user=%s msg=%s: %s", user.id, msg.message_id, exc)


def _build_input_media(msg: Message):
    common = {
        "caption": msg.caption,
        "caption_entities": msg.caption_entities,
    }
    if msg.photo:
        return InputMediaPhoto(media=msg.photo[-1].file_id, **common)
    if msg.video:
        return InputMediaVideo(media=msg.video.file_id, **common)
    if msg.document:
        return InputMediaDocument(media=msg.document.file_id, **common)
    if msg.audio:
        return InputMediaAudio(media=msg.audio.file_id, **common)
    if msg.animation:
        return InputMediaAnimation(media=msg.animation.file_id, **common)
    return None


async def edit_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message
    if not msg:
        return

    link = _lookup_group_link(context, msg.chat_id, msg.message_id)
    if not link or link.get("direction") != "out":
        return

    user_id = int(link["user_id"])
    user_msg_id = int(link["user_msg_id"])

    try:
        if msg.text is not None:
            await _call_with_retry(
                lambda: context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=user_msg_id,
                    text=msg.text,
                    entities=msg.entities,
                ),
                op_name="edit_user_text",
            )
        else:
            media = _build_input_media(msg)
            if media is not None:
                await _call_with_retry(
                    lambda: context.bot.edit_message_media(
                        chat_id=user_id,
                        message_id=user_msg_id,
                        media=media,
                    ),
                    op_name="edit_user_media",
                )
            elif msg.caption is not None:
                await _call_with_retry(
                    lambda: context.bot.edit_message_caption(
                        chat_id=user_id,
                        message_id=user_msg_id,
                        caption=msg.caption,
                        caption_entities=msg.caption_entities,
                    ),
                    op_name="edit_user_caption",
                )
            else:
                logging.info(
                    "skipping unsupported edit type chat=%s msg=%s",
                    msg.chat_id,
                    msg.message_id,
                )
                return
        logging.info(
            "synced support edit chat=%s msg=%s -> user=%s msg=%s",
            msg.chat_id,
            msg.message_id,
            user_id,
            user_msg_id,
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logging.error("sync support edit failed for user=%s msg=%s: %s", user_id, user_msg_id, exc)
        try:
            await _call_with_retry(
                lambda: context.bot.send_message(
                    chat_id=msg.chat_id,
                    text=f"同步编辑到用户失败: {exc}",
                    reply_parameters=ReplyParameters(
                        message_id=msg.message_id,
                        allow_sending_without_reply=True,
                    ),
                ),
                op_name="notify_edit_failed",
            )
        except TelegramError:
            logging.exception("failed to notify support chat about edit failure")
    except TelegramError as exc:
        logging.error("sync support edit failed for user=%s msg=%s: %s", user_id, user_msg_id, exc)
        try:
            await _call_with_retry(
                lambda: context.bot.send_message(
                    chat_id=msg.chat_id,
                    text=f"同步编辑到用户失败: {exc}",
                    reply_parameters=ReplyParameters(
                        message_id=msg.message_id,
                        allow_sending_without_reply=True,
                    ),
                ),
                op_name="notify_edit_failed",
            )
        except TelegramError:
            logging.exception("failed to notify support chat about edit failure")


def _pick_reaction(
    old_reaction: Sequence[ReactionType], new_reaction: Sequence[ReactionType]
) -> Optional[list[ReactionTypeEmoji]]:
    if not new_reaction:
        return []
    new_emojis = [r.emoji for r in new_reaction if isinstance(r, ReactionTypeEmoji)]
    if not new_emojis:
        # Custom or paid reactions cannot be set directly by a standard bot.
        return None
    old_emojis = [r.emoji for r in old_reaction if isinstance(r, ReactionTypeEmoji)]
    added = [emoji for emoji in new_emojis if emoji not in old_emojis]
    chosen = added[-1] if added else new_emojis[-1]
    return [ReactionTypeEmoji(chosen)]


async def sync_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mr = update.message_reaction
    if not mr:
        return
    if mr.user and (mr.user.is_bot or mr.user.id == context.bot.id):
        return

    support_chats = support_chat_ids()
    if mr.chat.id in support_chats:
        link = _lookup_group_link(context, mr.chat.id, mr.message_id)
        if not link:
            return
        target_chat_id = int(link["user_id"])
        target_msg_id = int(link["user_msg_id"])
        direction = "support->user"
    elif mr.chat.type == "private":
        link = _lookup_user_link(context, mr.chat.id, mr.message_id)
        if not link:
            return
        target_chat_id = int(link["chat_id"])
        target_msg_id = int(link["group_msg_id"])
        direction = "user->support"
    else:
        return

    payload = _pick_reaction(mr.old_reaction, mr.new_reaction)
    if payload is None:
        logging.info(
            "skipping non-standard reaction (%s) chat=%s msg=%s",
            direction,
            mr.chat.id,
            mr.message_id,
        )
        return

    try:
        await _call_with_retry(
            lambda: context.bot.set_message_reaction(
                chat_id=target_chat_id,
                message_id=target_msg_id,
                reaction=payload,
            ),
            op_name="set_message_reaction",
        )
        logging.info(
            "synced reaction (%s) chat=%s msg=%s -> chat=%s msg=%s reaction=%s",
            direction,
            mr.chat.id,
            mr.message_id,
            target_chat_id,
            target_msg_id,
            [r.emoji for r in payload],
        )
    except TelegramError as exc:
        logging.warning(
            "failed to sync reaction (%s) -> chat=%s msg=%s: %s",
            direction,
            target_chat_id,
            target_msg_id,
            exc,
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(
        "unhandled exception while processing update %r",
        update,
        exc_info=context.error,
    )
