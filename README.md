# Discord Restock Bot

This bot watches three Cortis product pages and sends a Discord message when one of them changes from sold out to in stock. It can also send an email alert through SMTP.

## Watched products

- https://cortisofficial.us/products/greengreen-bridge-ver-signed
- https://cortisofficial.us/products/greengreen-street-ver-signed
- https://cortisofficial.us/products/greengreen-studio-ver-signed

## What it does

- Checks the product pages every few minutes
- Detects when a product restocks
- Sends a notification to one Discord channel
- Optionally sends an email alert
- Stores the last known stock state in `state.json` so it does not spam repeat alerts

## Setup

1. Install Python 3.10 or newer.
2. Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in your values.
4. Create a Discord application and bot at [Discord Developer Portal](https://discord.com/developers/applications).
5. Enable the `MESSAGE CONTENT INTENT` only if you plan to add message commands later. This bot does not require it right now.
6. Invite the bot to your server with permissions to `View Channels` and `Send Messages`.
7. Run the bot:

```powershell
py bot.py
```

## Discord configuration

- `DISCORD_BOT_TOKEN`: your bot token
- `DISCORD_CHANNEL_ID`: channel where alerts should be posted
- `DISCORD_MENTION_ROLE_ID`: optional role ID to ping on restock

## Email configuration

Set `EMAIL_ENABLED=true` and fill in the SMTP settings if you want email alerts too.

For Gmail, use an app password instead of your normal account password.

## Notes

- The bot currently detects stock by reading the product page text and checking for sold-out markers.
- On April 8, 2026, all three watched pages showed `Sold out`.
- The first run records the current state without sending alerts. Alerts start on later checks when a product changes from sold out to in stock.

## Deploy On Railway

1. Push this project to GitHub.
2. In Railway, create a new project and choose `Deploy from GitHub repo`.
3. Select this repository.
4. In the service settings, set the start command to `python bot.py` if Railway does not detect it automatically.
5. Add these environment variables in Railway:

```text
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_CHANNEL_ID=your_channel_id
DISCORD_MENTION_ROLE_ID=
CHECK_INTERVAL_SECONDS=180
EMAIL_ENABLED=false
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
EMAIL_FROM=
EMAIL_TO=
```

6. Optional but recommended: create a Railway volume and mount it to `/data`.
7. If you use a volume, add:

```text
STATE_FILE_PATH=/data/state.json
```

This keeps the bot's stock cache after redeploys or restarts.
