import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("TELEGRAM_SUPPORT_CHAT_ID", "-1001234567890")

from telegram import (
    InputMediaPhoto,
    MessageEntity,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
)
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

import handlers


class BotHandlersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name)
        self.orig_map = handlers.MAP_PATH
        self.orig_links = handlers.LINKS_PATH
        self.orig_cfg = handlers.CONFIG_PATH
        handlers.MAP_PATH = base / "forward_map.json"
        handlers.LINKS_PATH = base / "message_links.json"
        handlers.CONFIG_PATH = base / "config.json"

    def tearDown(self) -> None:
        handlers.MAP_PATH = self.orig_map
        handlers.LINKS_PATH = self.orig_links
        handlers.CONFIG_PATH = self.orig_cfg
        self.tmpdir.cleanup()

    def _make_context(self):
        bot = SimpleNamespace(
            id=999999,
            send_message=AsyncMock(),
            copy_message=AsyncMock(),
            edit_message_text=AsyncMock(),
            edit_message_media=AsyncMock(),
            edit_message_caption=AsyncMock(),
            set_message_reaction=AsyncMock(),
            get_chat_member=AsyncMock(),
        )
        return SimpleNamespace(bot=bot, bot_data={}, user_data={})

    async def test_retry_on_network_and_retry_after(self) -> None:
        attempts = {"n": 0}

        async def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise NetworkError("Bad Gateway")
            if attempts["n"] == 2:
                raise RetryAfter(0)
            return "ok"

        with patch("handlers.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await handlers._call_with_retry(flaky, op_name="test_op")
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(sleep_mock.await_count, 2)

    async def test_no_retry_on_bad_request_or_forbidden(self) -> None:
        attempts = {"n": 0}

        async def bad():
            attempts["n"] += 1
            raise BadRequest("bad")

        with self.assertRaises(BadRequest):
            await handlers._call_with_retry(bad, op_name="bad_op")
        self.assertEqual(attempts["n"], 1)

        attempts["n"] = 0

        async def blocked():
            attempts["n"] += 1
            raise Forbidden("bot was blocked by the user")

        with self.assertRaises(Forbidden):
            await handlers._call_with_retry(blocked, op_name="blocked_op")
        self.assertEqual(attempts["n"], 1)

    async def test_timeout_retries_at_most_once(self) -> None:
        attempts = {"n": 0}

        async def slow():
            attempts["n"] += 1
            raise TimedOut("read timeout")

        with patch("handlers.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with self.assertRaises(TimedOut):
                await handlers._call_with_retry(slow, op_name="slow_op")
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(sleep_mock.await_count, 1)

    async def test_forward_and_reply_roundtrip_and_chained_reply(self) -> None:
        ctx = self._make_context()
        user = SimpleNamespace(id=111, full_name="Alice", username="alice")
        user_msg = SimpleNamespace(
            message_id=10,
            chat_id=111,
            text="hello",
            caption=None,
            forward=AsyncMock(return_value=SimpleNamespace(message_id=200)),
            copy=AsyncMock(),
            reply_text=AsyncMock(),
        )
        upd_in = SimpleNamespace(message=user_msg, effective_user=user)
        await handlers.forward_to_group(upd_in, ctx)

        self.assertEqual(handlers._lookup(ctx, 200), 111)
        link = handlers._lookup_user_link(ctx, 111, 10)
        self.assertEqual(link["group_msg_id"], 200)

        # Support replies to group message 200
        ctx.bot.copy_message.return_value = SimpleNamespace(message_id=50)
        support_msg = SimpleNamespace(
            message_id=201,
            reply_to_message=SimpleNamespace(message_id=200, reply_to_message=None),
            reply_text=AsyncMock(),
        )
        upd_out = SimpleNamespace(
            message=support_msg,
            effective_chat=SimpleNamespace(id=-1001234567890),
        )
        await handlers.forward_to_user(upd_out, ctx)

        ctx.bot.copy_message.assert_awaited_once_with(
            chat_id=111,
            from_chat_id=-1001234567890,
            message_id=201,
        )
        # Support can also chain-reply to their own message 201
        self.assertEqual(handlers._lookup(ctx, 201), 111)
        out_link = handlers._lookup_group_link(ctx, -1001234567890, 201)
        self.assertEqual(out_link["user_msg_id"], 50)
        self.assertEqual(out_link["direction"], "out")

    async def test_user_edit_text_with_utf16_entities(self) -> None:
        ctx = self._make_context()
        await handlers._remember_link(
            ctx,
            group_chat_id=-1001234567890,
            group_message_id=300,
            user_id=222,
            user_message_id=20,
            direction="in",
        )
        ctx.bot.send_message.return_value = SimpleNamespace(message_id=301)

        ent = MessageEntity(type=MessageEntity.BOLD, offset=0, length=4)
        edited = SimpleNamespace(
            message_id=20,
            chat_id=222,
            text="bold text",
            entities=[ent],
        )
        upd = SimpleNamespace(
            edited_message=edited,
            effective_user=SimpleNamespace(id=222, full_name="Bob", username=None),
        )
        await handlers.edit_to_group(upd, ctx)

        call_kwargs = ctx.bot.send_message.await_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], -1001234567890)
        self.assertTrue(call_kwargs["text"].startswith("✏️ 用户编辑了消息：\n"))
        self.assertEqual(call_kwargs["reply_parameters"].message_id, 300)
        expected_delta = len("✏️ 用户编辑了消息：\n".encode("utf-16-le")) // 2
        self.assertEqual(call_kwargs["entities"][0].offset, expected_delta)
        # Both old (300) and new (301) group messages map to user 222
        self.assertEqual(handlers._lookup(ctx, 301), 222)
        self.assertEqual(handlers._lookup_user_link(ctx, 222, 20)["group_msg_id"], 301)

    async def test_support_edit_text_and_media_in_place(self) -> None:
        ctx = self._make_context()
        await handlers._remember_link(
            ctx,
            group_chat_id=-1001234567890,
            group_message_id=400,
            user_id=333,
            user_message_id=70,
            direction="out",
        )

        # Text edit
        upd_text = SimpleNamespace(
            edited_message=SimpleNamespace(
                chat_id=-1001234567890,
                message_id=400,
                text="updated reply",
                entities=None,
                photo=None,
                video=None,
                document=None,
                audio=None,
                animation=None,
                caption=None,
                caption_entities=None,
            )
        )
        await handlers.edit_to_user(upd_text, ctx)
        ctx.bot.edit_message_text.assert_awaited_once_with(
            chat_id=333,
            message_id=70,
            text="updated reply",
            entities=None,
        )

        # Photo/caption edit
        upd_photo = SimpleNamespace(
            edited_message=SimpleNamespace(
                chat_id=-1001234567890,
                message_id=400,
                text=None,
                entities=None,
                photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
                video=None,
                document=None,
                audio=None,
                animation=None,
                caption="new cap",
                caption_entities=None,
            )
        )
        await handlers.edit_to_user(upd_photo, ctx)
        media_arg = ctx.bot.edit_message_media.await_args.kwargs["media"]
        self.assertIsInstance(media_arg, InputMediaPhoto)
        self.assertEqual(media_arg.media, "large")
        self.assertEqual(media_arg.caption, "new cap")

    async def test_support_edit_ignores_not_modified(self) -> None:
        ctx = self._make_context()
        await handlers._remember_link(
            ctx,
            group_chat_id=-1001234567890,
            group_message_id=401,
            user_id=333,
            user_message_id=71,
            direction="out",
        )
        ctx.bot.edit_message_text.side_effect = BadRequest("Message is not modified")
        upd = SimpleNamespace(
            edited_message=SimpleNamespace(
                chat_id=-1001234567890,
                message_id=401,
                text="same",
                entities=None,
            )
        )
        await handlers.edit_to_user(upd, ctx)
        ctx.bot.send_message.assert_not_awaited()

    async def test_bidirectional_reaction_sync_and_clear(self) -> None:
        ctx = self._make_context()
        await handlers._remember_link(
            ctx,
            group_chat_id=-1001234567890,
            group_message_id=500,
            user_id=444,
            user_message_id=80,
            direction="in",
        )

        # Support reacts with 👍 -> synced to user msg 80
        mr_support = SimpleNamespace(
            chat=SimpleNamespace(id=-1001234567890, type="supergroup"),
            message_id=500,
            user=SimpleNamespace(id=732550736, is_bot=False),
            old_reaction=[],
            new_reaction=[ReactionTypeEmoji("👍")],
        )
        await handlers.sync_reaction(SimpleNamespace(message_reaction=mr_support), ctx)
        ctx.bot.set_message_reaction.assert_awaited_once_with(
            chat_id=444,
            message_id=80,
            reaction=[ReactionTypeEmoji("👍")],
        )

        # User reacts with ❤️ -> synced to support msg 500
        ctx.bot.set_message_reaction.reset_mock()
        mr_user = SimpleNamespace(
            chat=SimpleNamespace(id=444, type="private"),
            message_id=80,
            user=SimpleNamespace(id=444, is_bot=False),
            old_reaction=[ReactionTypeEmoji("👍")],
            new_reaction=[ReactionTypeEmoji("👍"), ReactionTypeEmoji("❤️")],
        )
        await handlers.sync_reaction(SimpleNamespace(message_reaction=mr_user), ctx)
        ctx.bot.set_message_reaction.assert_awaited_once_with(
            chat_id=-1001234567890,
            message_id=500,
            reaction=[ReactionTypeEmoji("❤️")],
        )

        # User clears reaction -> synced []
        ctx.bot.set_message_reaction.reset_mock()
        mr_clear = SimpleNamespace(
            chat=SimpleNamespace(id=444, type="private"),
            message_id=80,
            user=SimpleNamespace(id=444, is_bot=False),
            old_reaction=[ReactionTypeEmoji("❤️")],
            new_reaction=[],
        )
        await handlers.sync_reaction(SimpleNamespace(message_reaction=mr_clear), ctx)
        ctx.bot.set_message_reaction.assert_awaited_once_with(
            chat_id=-1001234567890,
            message_id=500,
            reaction=[],
        )

        # Custom emoji only -> skipped without calling API
        ctx.bot.set_message_reaction.reset_mock()
        mr_custom = SimpleNamespace(
            chat=SimpleNamespace(id=444, type="private"),
            message_id=80,
            user=SimpleNamespace(id=444, is_bot=False),
            old_reaction=[],
            new_reaction=[ReactionTypeCustomEmoji("5368324170671202286")],
        )
        await handlers.sync_reaction(SimpleNamespace(message_reaction=mr_custom), ctx)
        ctx.bot.set_message_reaction.assert_not_awaited()

        # Bot's own reaction -> ignored to prevent loops
        mr_bot = SimpleNamespace(
            chat=SimpleNamespace(id=-1001234567890, type="supergroup"),
            message_id=500,
            user=SimpleNamespace(id=999999, is_bot=True),
            old_reaction=[],
            new_reaction=[ReactionTypeEmoji("🔥")],
        )
        await handlers.sync_reaction(SimpleNamespace(message_reaction=mr_bot), ctx)
        ctx.bot.set_message_reaction.assert_not_awaited()

    async def test_user_edit_media_copies_and_tags(self) -> None:
        ctx = self._make_context()
        await handlers._remember_link(
            ctx,
            group_chat_id=-1001234567890,
            group_message_id=600,
            user_id=555,
            user_message_id=90,
            direction="in",
        )
        ctx.bot.copy_message.return_value = SimpleNamespace(message_id=601)
        ctx.bot.send_message.return_value = SimpleNamespace(message_id=602)

        edited = SimpleNamespace(
            message_id=90,
            chat_id=555,
            text=None,
            caption="edited caption",
        )
        upd = SimpleNamespace(
            edited_message=edited,
            effective_user=SimpleNamespace(id=555, full_name="Carol", username="carol"),
        )
        await handlers.edit_to_group(upd, ctx)

        ctx.bot.copy_message.assert_awaited_once()
        self.assertEqual(ctx.bot.copy_message.await_args.kwargs["reply_parameters"].message_id, 600)
        self.assertEqual(handlers._lookup(ctx, 601), 555)
        self.assertEqual(handlers._lookup(ctx, 602), 555)

    async def test_legacy_forward_map_compatibility_and_main_import(self) -> None:
        handlers.MAP_PATH.write_text('{"221": 6214507892}', encoding="utf-8")
        ctx = self._make_context()
        ctx.bot.copy_message.return_value = SimpleNamespace(message_id=99)
        support_msg = SimpleNamespace(
            message_id=222,
            reply_to_message=SimpleNamespace(message_id=221, reply_to_message=None),
            reply_text=AsyncMock(),
        )
        upd = SimpleNamespace(
            message=support_msg,
            effective_chat=SimpleNamespace(id=-1001234567890),
        )
        await handlers.forward_to_user(upd, ctx)
        ctx.bot.copy_message.assert_awaited_once_with(
            chat_id=6214507892,
            from_chat_id=-1001234567890,
            message_id=222,
        )

        import main as bot_main

        self.assertTrue(callable(bot_main.main))


if __name__ == "__main__":
    unittest.main()
