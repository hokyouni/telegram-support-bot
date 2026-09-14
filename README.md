# telegram-support-bot

Fork of [ohld/telegram-support-bot](https://github.com/ohld/telegram-support-bot): a Telegram bot that forwards private messages to a support chat and copies replies back to the user.

This fork adds Docker, a public GHCR image, Watchtower-friendly tags, persistent reply mapping, and forwarding for media as well as text.

Image: `ghcr.io/hokyouni/telegram-support-bot:latest` (`linux/amd64`, `linux/arm64`)

## How it works

1. A user messages the bot privately.
2. The bot forwards the message to your support chat.
3. Reply to that forwarded message in the support chat.
4. The bot copies the reply back to the user.

## Environment

Copy `.env.example` to `.env`. Token and chat IDs stay on the host; they are not in the image.

```bash
TELEGRAM_TOKEN=                 # from @BotFather
TELEGRAM_SUPPORT_CHAT_ID=       # group/supergroup id, usually -100...
PERSONAL_ACCOUNT_CHAT_ID=       # optional; defaults to the support chat
FORWARD_MODE=support_chat       # or personal_account
WELCOME_MESSAGE=你好，请直接发送消息，我们会尽快回复。
```

The bot must be a member of the support chat (admin recommended). Staff must **reply** to the forwarded message.

Support-group administrators can change the `/start` welcome text in a private chat with the bot:

- `/welcome` — show the current text
- `/setwelcome` — then send the new text, or `/setwelcome 新文案`
- `/cancel` — abort

The live text is stored in `/data/config.json`. `WELCOME_MESSAGE` is only the fallback if nothing has been set yet. Extra user IDs can be added with `ADMIN_USER_IDS`.

## Docker

```bash
cp .env.example .env
# fill TELEGRAM_TOKEN and TELEGRAM_SUPPORT_CHAT_ID
mkdir -p data
docker compose pull
docker compose up -d
```

The compose file publishes no ports (long polling only), drops capabilities, uses a read-only root filesystem, and sets `com.centurylinklabs.watchtower.enable=true`.

Watchtower can pull `ghcr.io/hokyouni/telegram-support-bot:latest` on hosts that already watch labeled containers.

## Run without Docker

```bash
pip install -r requirements.txt
python main.py
```
