# Tunefilx Telegram Clip Bot

This bot downloads a selected clip from a public YouTube video and sends it back on Telegram.

## Setup

1. Install Python packages:

```powershell
& 'C:\Users\yugant\AppData\Local\Programs\Python\Python313\python.exe' -m pip install -r requirements.txt
```

2. Copy `.env.example` to `.env`, then add your Telegram bot token:

```powershell
Copy-Item .env.example .env
notepad .env
```

3. Run the bot:

```powershell
& 'C:\Users\yugant\AppData\Local\Programs\Python\Python313\python.exe' bot.py
```

## Use

Send a YouTube link to the bot:

```text
https://youtu.be/VIDEO_ID
```

The bot will ask for the start point, then the end point, then it will send the clip.

You can also send `/clip` to start manually.

Time formats:

- `10` means 10 seconds
- `01:20` means 1 minute 20 seconds
- `00:01:20` means 1 minute 20 seconds

Note: The YouTube video must be public. The clip must be under 49 MB for Telegram upload.

## Render Deployment

Use these settings on Render:

Build Command:

```bash
bash render-build.sh
```

Start Command:

```bash
python bot.py
```

Environment variables:

```text
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
```

If YouTube shows "Sign in to confirm you're not a bot", add YouTube cookies.

Recommended option:

```text
YOUTUBE_COOKIES_B64=base64_encoded_netscape_cookies_file
```

Alternative option:

```text
YOUTUBE_COOKIES=paste_cookies_txt_content_here
```

Do not share your cookies publicly. Cookies can expire, so you may need to update them later.
