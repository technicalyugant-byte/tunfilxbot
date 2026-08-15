import asyncio
import base64
import importlib.util
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from telegram import Update
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters


def load_env_file() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAX_TELEGRAM_UPLOAD_MB = 49
TIME_RE = re.compile(r"^\d{1,2}(:\d{1,2}){0,2}$")
DOWNLOAD_PERCENT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
ASK_LINK, ASK_START, ASK_END, ASK_UPLOAD_START, ASK_UPLOAD_END = range(5)


def parse_time(value: str) -> int:
    if not TIME_RE.match(value):
        raise ValueError("Invalid time format.")

    parts = [int(part) for part in value.split(":")]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        minutes, seconds = parts
        if seconds >= 60:
            raise ValueError("Seconds must be less than 60.")
        return minutes * 60 + seconds

    hours, minutes, seconds = parts
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Minutes and seconds must be less than 60.")
    return hours * 3600 + minutes * 60 + seconds


def format_time(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_progress_message(phase: str, percent: float | None, frame: str) -> str:
    if percent is None:
        return f"{phase} {frame}\nProgress: starting..."

    percent = max(0, min(100, percent))
    filled = int(percent // 10)
    bar = "#" * filled + "-" * (10 - filled)
    return f"{phase} {frame}\nProgress: [{bar}] {percent:.1f}%"


async def safe_edit_text(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise
    except RetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
    except TimedOut:
        pass


def get_ffmpeg_location() -> str | None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


def get_js_runtime_arg() -> str | None:
    deno_path = shutil.which("deno")
    if deno_path:
        return f"deno:{deno_path}"

    project_deno_path = Path.cwd() / ".deno" / "bin" / "deno"
    if project_deno_path.exists():
        return f"deno:{project_deno_path}"

    render_deno_path = Path.home() / ".deno" / "bin" / "deno"
    if render_deno_path.exists():
        return f"deno:{render_deno_path}"

    node_path = shutil.which("node")
    if node_path:
        return f"node:{node_path}"

    return None


def get_youtube_cookies_file(temp_dir: str) -> Path | None:
    cookies_file = os.getenv("YOUTUBE_COOKIES_FILE")
    if cookies_file and Path(cookies_file).exists():
        return Path(cookies_file)

    cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64")
    if cookies_b64:
        cookies_path = Path(temp_dir) / "youtube_cookies.txt"
        cookies_path.write_bytes(base64.b64decode(cookies_b64))
        return cookies_path

    cookies_text = os.getenv("YOUTUBE_COOKIES")
    if cookies_text:
        cookies_path = Path(temp_dir) / "youtube_cookies.txt"
        cookies_path.write_text(cookies_text.replace("\\n", "\n"), encoding="utf-8")
        return cookies_path

    return None


def add_youtube_options(command: list[str], cookies_file: Path | None) -> None:
    js_runtime = get_js_runtime_arg()
    if js_runtime:
        command.extend(["--js-runtimes", js_runtime])

    if cookies_file:
        command.extend(["--cookies", str(cookies_file)])


def build_download_error(output: str) -> str:
    if "Sign in to confirm" in output or "--cookies-from-browser or --cookies" in output:
        return (
            "YouTube is blocking this server as a bot.\n\n"
            "Fix on Render:\n"
            "1. Export YouTube cookies as a Netscape cookies.txt file.\n"
            "2. Convert it to base64.\n"
            "3. Add it on Render as YOUTUBE_COOKIES_B64.\n"
            "4. Redeploy the service.\n\n"
            "Do not share your cookies publicly."
        )

    return (
        "The clip could not be downloaded.\n"
        "Make sure the link is public and the time range is valid.\n\n"
        f"Error: {output[-800:]}"
    )


def ensure_tools_available() -> str:
    if importlib.util.find_spec("yt_dlp") is None:
        raise RuntimeError("Missing tool: yt-dlp. Install it with: python -m pip install -r requirements.txt")

    ffmpeg_location = get_ffmpeg_location()
    if ffmpeg_location is None:
        raise RuntimeError("Missing tool: ffmpeg. Install it with: python -m pip install -r requirements.txt")

    return ffmpeg_location


async def run_command_with_progress(command: list[str], status_message) -> tuple[int, str]:
    state = {
        "phase": "Preparing your clip",
        "percent": None,
        "lines": [],
    }

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def read_output():
        while True:
            raw_line = await process.stdout.readline()
            if not raw_line:
                break

            line = raw_line.decode(errors="ignore").strip()
            if not line:
                continue

            state["lines"].append(line)
            state["lines"] = state["lines"][-30:]

            percent_match = DOWNLOAD_PERCENT_RE.search(line)
            if percent_match:
                state["phase"] = "Downloading clip"
                state["percent"] = float(percent_match.group(1))
            elif line.startswith("[download] Destination"):
                state["phase"] = "Starting download"
            elif line.startswith("[Merger]") or line.startswith("[VideoRemuxer]") or line.startswith("[Fixup"):
                state["phase"] = "Processing clip"

    async def animate_progress():
        frames = ["|", "/", "-", "\\"]
        index = 0
        last_text = ""
        while process.returncode is None:
            text = format_progress_message(state["phase"], state["percent"], frames[index % len(frames)])
            if text != last_text:
                await safe_edit_text(status_message, text)
                last_text = text
            index += 1
            await asyncio.sleep(2)

    reader_task = asyncio.create_task(read_output())
    animator_task = asyncio.create_task(animate_progress())

    return_code = await process.wait()
    await reader_task
    animator_task.cancel()
    await asyncio.gather(animator_task, return_exceptions=True)

    if return_code == 0:
        await safe_edit_text(status_message, format_progress_message("Finalizing clip", 100, "|"))

    return return_code, "\n".join(state["lines"])


async def run_command(command: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode, output.decode(errors="ignore").strip()


async def download_full_video(
    url: str,
    temp_dir: str,
    ffmpeg_location: str,
    cookies_file: Path | None,
    status_message,
) -> tuple[Path | None, str]:
    output_template = str(Path(temp_dir) / "source.%(ext)s")
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--newline",
        "--no-playlist",
        "--ffmpeg-location",
        ffmpeg_location,
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b",
        "-o",
        output_template,
    ]
    add_youtube_options(command, cookies_file)
    command.append(url)

    await safe_edit_text(status_message, "Direct clipping failed. Downloading video for local processing...")
    return_code, output = await run_command_with_progress(command, status_message)
    if return_code != 0:
        return None, output

    files = sorted(Path(temp_dir).glob("source.*"), key=lambda path: path.stat().st_size, reverse=True)
    if not files:
        return None, "The source video file was not created."

    return files[0], output


async def cut_local_clip(
    source_file: Path,
    output_file: Path,
    ffmpeg_location: str,
    start_seconds: int,
    end_seconds: int,
    status_message,
) -> tuple[int, str]:
    await safe_edit_text(status_message, "Cutting clip locally...\nProgress: [########--] 80.0%")
    command = [
        ffmpeg_location,
        "-y",
        "-ss",
        format_time(start_seconds),
        "-i",
        str(source_file),
        "-t",
        format_time(end_seconds - start_seconds),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output_file),
    ]
    return await run_command(command)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! Welcome to Tunefilx.\n\n"
        "Send a YouTube video link or upload a video file.\n"
        "I will ask for the start point and end point."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def clip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Send the YouTube video link or upload a video file.")
    return ASK_LINK


async def receive_video_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    document = update.message.document

    if video:
        file_id = video.file_id
        file_name = "uploaded_video.mp4"
        file_size = video.file_size
    elif document and document.mime_type and document.mime_type.startswith("video/"):
        file_id = document.file_id
        file_name = document.file_name or "uploaded_video.mp4"
        file_size = document.file_size
    else:
        await update.message.reply_text("Please upload a valid video file.")
        return ASK_LINK

    context.user_data.clear()
    context.user_data["upload_file_id"] = file_id
    context.user_data["upload_file_name"] = file_name
    context.user_data["upload_file_size"] = file_size
    await update.message.reply_text("Video received. Send the start point.\nExample: 10, 01:20, or 00:01:20")
    return ASK_UPLOAD_START


async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "youtube.com/" not in url and "youtu.be/" not in url:
        await update.message.reply_text("Please send a valid YouTube link.")
        return ASK_LINK

    context.user_data["url"] = url
    await update.message.reply_text("Send the start point.\nExample: 10, 01:20, or 00:01:20")
    return ASK_START


async def receive_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_raw = update.message.text.strip()

    try:
        start_seconds = parse_time(start_raw)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\nUse: 10, 01:20, or 00:01:20")
        return ASK_START

    context.user_data["start_seconds"] = start_seconds
    await update.message.reply_text("Send the end point.\nExample: 02:10")
    return ASK_END


async def receive_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_raw = update.message.text.strip()

    try:
        start_seconds = parse_time(start_raw)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\nUse: 10, 01:20, or 00:01:20")
        return ASK_UPLOAD_START

    context.user_data["start_seconds"] = start_seconds
    await update.message.reply_text("Send the end point.\nExample: 02:10")
    return ASK_UPLOAD_END


async def receive_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    end_raw = update.message.text.strip()

    try:
        end_seconds = parse_time(end_raw)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\nUse: 10, 01:20, or 00:01:20")
        return ASK_END

    url = context.user_data["url"]
    start_seconds = context.user_data["start_seconds"]

    if start_seconds >= end_seconds:
        await update.message.reply_text("The end point must be after the start point.")
        return ASK_END

    await download_and_send_clip(update, url, start_seconds, end_seconds)
    context.user_data.clear()
    return ConversationHandler.END


async def receive_upload_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    end_raw = update.message.text.strip()

    try:
        end_seconds = parse_time(end_raw)
    except ValueError as exc:
        await update.message.reply_text(f"{exc}\nUse: 10, 01:20, or 00:01:20")
        return ASK_UPLOAD_END

    start_seconds = context.user_data["start_seconds"]

    if start_seconds >= end_seconds:
        await update.message.reply_text("The end point must be after the start point.")
        return ASK_UPLOAD_END

    await cut_uploaded_video(update, context, start_seconds, end_seconds)
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Okay, the clip request has been cancelled.")
    return ConversationHandler.END


async def download_and_send_clip(update: Update, url: str, start_seconds: int, end_seconds: int):
    try:
        ffmpeg_location = ensure_tools_available()
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return

    status_message = await update.message.reply_text("Creating your clip. Please wait...")

    with tempfile.TemporaryDirectory(prefix="tunefilx_") as temp_dir:
        cookies_file = get_youtube_cookies_file(temp_dir)
        output_template = str(Path(temp_dir) / "clip.%(ext)s")
        section = f"*{format_time(start_seconds)}-{format_time(end_seconds)}"

        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--newline",
            "--no-playlist",
            "--download-sections",
            section,
            "--force-keyframes-at-cuts",
            "--ffmpeg-location",
            ffmpeg_location,
            "--merge-output-format",
            "mp4",
            "-f",
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
            "-o",
            output_template,
        ]
        add_youtube_options(command, cookies_file)
        command.append(url)

        return_code, output = await run_command_with_progress(command, status_message)
        used_fallback = False

        if return_code != 0:
            source_file, fallback_output = await download_full_video(
                url,
                temp_dir,
                ffmpeg_location,
                cookies_file,
                status_message,
            )
            if source_file is None:
                await status_message.edit_text(build_download_error(fallback_output))
                return

            local_clip = Path(temp_dir) / "clip_local.mp4"
            cut_code, cut_output = await cut_local_clip(
                source_file,
                local_clip,
                ffmpeg_location,
                start_seconds,
                end_seconds,
                status_message,
            )
            if cut_code != 0:
                await status_message.edit_text(
                    "The clip could not be processed.\n"
                    "Try a shorter clip or a different time range.\n\n"
                    f"Error: {cut_output[-800:]}"
                )
                return
            used_fallback = True

        files = sorted(Path(temp_dir).glob("clip.*"), key=lambda path: path.stat().st_size, reverse=True)
        if used_fallback:
            files = [Path(temp_dir) / "clip_local.mp4"]
        if not files:
            await status_message.edit_text("The clip file was not created. Try another link or time range.")
            return

        clip_file = files[0]
        file_size_mb = clip_file.stat().st_size / (1024 * 1024)
        if file_size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await status_message.edit_text(
                f"The clip was created, but its size is {file_size_mb:.1f} MB. "
                f"Please make a shorter clip under {MAX_TELEGRAM_UPLOAD_MB} MB for Telegram upload."
            )
            return

        await status_message.edit_text("Clip is ready. Uploading now...")
        with clip_file.open("rb") as video:
            await update.message.reply_video(
                video=video,
                caption=f"Clip: {format_time(start_seconds)} - {format_time(end_seconds)}",
                supports_streaming=True,
            )
        await status_message.delete()


async def cut_uploaded_video(update: Update, context: ContextTypes.DEFAULT_TYPE, start_seconds: int, end_seconds: int):
    try:
        ffmpeg_location = ensure_tools_available()
    except RuntimeError as exc:
        await update.message.reply_text(str(exc))
        return

    status_message = await update.message.reply_text("Downloading your uploaded video...")

    with tempfile.TemporaryDirectory(prefix="tunefilx_upload_") as temp_dir:
        file_name = Path(context.user_data["upload_file_name"]).name
        source_file = Path(temp_dir) / file_name
        output_file = Path(temp_dir) / "uploaded_clip.mp4"

        telegram_file = await context.bot.get_file(context.user_data["upload_file_id"])
        await telegram_file.download_to_drive(custom_path=source_file)

        cut_code, cut_output = await cut_local_clip(
            source_file,
            output_file,
            ffmpeg_location,
            start_seconds,
            end_seconds,
            status_message,
        )
        if cut_code != 0:
            await status_message.edit_text(
                "The uploaded video could not be clipped.\n"
                "Try a shorter clip or a different time range.\n\n"
                f"Error: {cut_output[-800:]}"
            )
            return

        file_size_mb = output_file.stat().st_size / (1024 * 1024)
        if file_size_mb > MAX_TELEGRAM_UPLOAD_MB:
            await status_message.edit_text(
                f"The clip was created, but its size is {file_size_mb:.1f} MB. "
                f"Please make a shorter clip under {MAX_TELEGRAM_UPLOAD_MB} MB for Telegram upload."
            )
            return

        await status_message.edit_text("Clip is ready. Uploading now...")
        with output_file.open("rb") as video:
            await update.message.reply_video(
                video=video,
                caption=f"Clip: {format_time(start_seconds)} - {format_time(end_seconds)}",
                supports_streaming=True,
            )
        await status_message.delete()


def main():
    if not TOKEN:
        raise RuntimeError("Set the TELEGRAM_BOT_TOKEN environment variable or add it to the .env file.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("clip", clip),
                MessageHandler(filters.Regex(r"(youtube\.com/|youtu\.be/)") & ~filters.COMMAND, receive_link),
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video_upload),
            ],
            states={
                ASK_LINK: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link),
                    MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video_upload),
                ],
                ASK_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_start)],
                ASK_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_end)],
                ASK_UPLOAD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_upload_start)],
                ASK_UPLOAD_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_upload_end)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
