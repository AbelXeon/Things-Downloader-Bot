import os
import re
import shutil
import tempfile

import telebot
from telebot import apihelper
from dotenv import load_dotenv
import yt_dlp
import imageio_ffmpeg

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN. Please set it as an Environment Variable!")

apihelper.CONNECT_TIMEOUT = 120
apihelper.READ_TIMEOUT = 300

# 8 worker threads = 8 users can download at the same time
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)

MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram Bot API upload limit
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
HAS_ARIA2 = shutil.which("aria2c") is not None

URL_RE = re.compile(r"https?://\S+")

# Prefer single-file mp4 (no merge = fastest). Every step skips files known to be >50MB.
# "?" means: also accept if the size is unknown.
FORMAT = (
    "best[height<=720][ext=mp4][filesize<50M?][filesize_approx<50M?]/"
    "bv*[height<=720][ext=mp4][filesize<50M?]+ba[ext=m4a]/"
    "best[height<=480][filesize<50M?][filesize_approx<50M?]/"
    "best[height<=360]/"
    "best"
)


def build_opts(workdir):
    opts = {
        "ffmpeg_location": FFMPEG_PATH,
        "format": FORMAT,
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": MAX_FILE_SIZE,
        "concurrent_fragment_downloads": 16,
        "http_chunk_size": 10 * 1024 * 1024,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    if HAS_ARIA2:
        opts["external_downloader"] = {"default": "aria2c"}
        opts["external_downloader_args"] = {"aria2c": ["-x16", "-s16", "-k1M"]}
    return opts


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(
        message,
        "👋 *Universal Fast Video Downloader*\n\n"
        "Send me a link from:\n"
        "• YouTube / Shorts\n"
        "• TikTok\n"
        "• Instagram\n"
        "• X (Twitter)\n"
        "• Facebook / Reddit\n\n"
        "⚡ Just paste the link below!",
        parse_mode="Markdown",
    )


@bot.message_handler(func=lambda m: m.text and URL_RE.search(m.text))
def handle_link(message):
    url = URL_RE.search(message.text).group(0)
    chat_id = message.chat.id
    status = bot.reply_to(message, "⚡ Downloading...")
    workdir = tempfile.mkdtemp(prefix="dl_")

    def edit(text):
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=status.message_id)
        except Exception:
            pass

    try:
        with yt_dlp.YoutubeDL(build_opts(workdir)) as ydl:
            info = ydl.extract_info(url, download=True)

        title = (info.get("title") or "Video")[:200]

        files = [
            os.path.join(workdir, f)
            for f in os.listdir(workdir)
            if not f.endswith((".part", ".ytdl", ".temp"))
        ]
        if not files:
            edit("❌ This video is larger than 50 MB.")
            return

        path = max(files, key=os.path.getsize)

        if os.path.getsize(path) > MAX_FILE_SIZE:
            edit("❌ This video is larger than 50 MB.")
            return

        edit("🚀 Uploading...")

        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            if ext in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
                bot.send_video(
                    chat_id,
                    video=f,
                    caption=f"🎬 {title}",
                    supports_streaming=True,
                    width=info.get("width"),
                    height=info.get("height"),
                    duration=int(info["duration"]) if info.get("duration") else None,
                    timeout=300,
                )
            elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                bot.send_photo(chat_id, photo=f, caption=title, timeout=300)
            else:
                bot.send_document(chat_id, document=f, caption=title, timeout=300)

        try:
            bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass

    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "larger than" in msg or "max-filesize" in msg:
            edit("❌ This video is larger than 50 MB.")
        else:
            edit("❌ Failed to download. The link might be private, blocked, or age-restricted.")
    except Exception as e:
        edit(f"❌ An error occurred: {str(e)[:100]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@bot.message_handler(func=lambda m: True)
def handle_other(message):
    bot.reply_to(message, "Please send a valid video URL starting with http:// or https://")


if __name__ == "__main__":
    print("🤖 Fast Bot is running...")
    bot.infinity_polling(skip_pending=True)