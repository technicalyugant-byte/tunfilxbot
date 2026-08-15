# Tunefilx — Cloudflare Worker scaffold

This folder is a deployable Cloudflare Worker webhook scaffold.

Important: the uploaded `bot.py` is a long-running Python Telegram bot. It uses
`app.run_polling()`, yt-dlp, subprocesses and FFmpeg. It cannot be made into a
working Cloudflare Worker simply by adding `wrangler.toml`.

This Worker can receive Telegram webhooks, but actual YouTube clipping requires
a separate processing backend that can run yt-dlp + FFmpeg.

## Cloudflare settings

Build command:
    (leave empty)

Deploy command:
    npx wrangler deploy

Root directory:
    /

## Variables / Secrets

Add these in Cloudflare:
- TELEGRAM_BOT_TOKEN
- VIDEO_BACKEND_URL (only if you have a separate processing backend)

Do NOT put Telegram tokens or YouTube cookies in this repository.

## Webhook

After deployment, set Telegram's webhook to:
    https://YOUR-WORKER-DOMAIN/telegram/webhook

Do not deploy the old `bot.py` as the Worker entry point.
