import os
import re
import html
import time
import shutil
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot import apihelper, types
from dotenv import load_dotenv
import yt_dlp
import imageio_ffmpeg

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN. Please set it as an Environment Variable!")

apihelper.CONNECT_TIMEOUT = 120
apihelper.READ_TIMEOUT = 300

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)

MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram Bot API upload limit
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
URL_RE = re.compile(r"https?://\S+")

PENDING = {}  # key -> {"url", "t", "busy"}
PENDING_LOCK = threading.Lock()
PENDING_TTL = 60 * 60  # 1 hour

COOKIES_FILE = None


# ───────────────────────── cookies (for YouTube / Instagram) ─────────────────────────
def setup_cookies():
    """Render secret file (/etc/secrets/cookies.txt) or COOKIES env var -> writable file."""
    global COOKIES_FILE
    target = os.path.join(tempfile.gettempdir(), "cookies.txt")
    secret = "/etc/secrets/cookies.txt"
    if os.path.exists(secret):
        shutil.copy(secret, target)
        COOKIES_FILE = target
    elif os.getenv("COOKIES"):
        with open(target, "w", encoding="utf-8") as f:
            f.write(os.getenv("COOKIES"))
        COOKIES_FILE = target
    elif os.path.exists("cookies.txt"):
        shutil.copy("cookies.txt", target)
        COOKIES_FILE = target


# ───────────────────────── health server (Render needs an open port) ─────────────────────────
class Health(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"Bot is running")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ───────────────────────── helpers ─────────────────────────
def esc(text):
    return html.escape(str(text or ""))


def fmt_duration(seconds):
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def set_text(msg, text, markup=None):
    """Edit a message whether it is a photo card (caption) or a plain text message."""
    try:
        if msg.content_type == "photo":
            bot.edit_message_caption(
                text, msg.chat.id, msg.message_id, parse_mode="HTML", reply_markup=markup
            )
        else:
            bot.edit_message_text(
                text, msg.chat.id, msg.message_id, parse_mode="HTML", reply_markup=markup
            )
    except Exception:
        pass


def friendly_error(e):
    m = str(e).lower()
    if "sign in to confirm" in m or "not a bot" in m:
        return (
            "⚠️ <b>YouTube blocked this server.</b>\n"
            "The bot admin needs to add YouTube cookies (see README)."
        )
    if "login" in m or "log in" in m or "cookies" in m or "private" in m:
        return "🔒 This content is private or needs a login."
    if "age" in m and "restrict" in m:
        return "🔞 This video is age-restricted."
    if "unsupported url" in m:
        return "❓ This link isn't supported."
    if "429" in m or "rate" in m:
        return "⏳ Rate limited by the site. Try again in a minute."
    return "❌ Failed to download. The link might be private, blocked, or removed."


def fmt_selector(q):
    if q == "audio":
        return "bestaudio[ext=m4a]/bestaudio/best"
    h = int(q)
    return (
        f"best[height<={h}][ext=mp4][filesize<?50M][filesize_approx<?50M]/"
        f"bv*[height<={h}][ext=mp4][filesize<?50M]+ba[ext=m4a]/"
        f"best[height<={h}][filesize<?50M][filesize_approx<?50M]/"
        f"best[height<={h}]/"
        "best"
    )


def base_opts():
    opts = {
        "ffmpeg_location": FFMPEG_PATH,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    return opts


def prune_pending():
    now = time.time()
    with PENDING_LOCK:
        for k in [k for k, v in PENDING.items() if now - v["t"] > PENDING_TTL]:
            PENDING.pop(k, None)


def quality_keyboard(key):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎬 720p  ⚡", callback_data=f"dl|{key}|720"),
        types.InlineKeyboardButton("🎬 480p", callback_data=f"dl|{key}|480"),
    )
    kb.add(
        types.InlineKeyboardButton("🎬 360p", callback_data=f"dl|{key}|360"),
        types.InlineKeyboardButton("🎵 Audio", callback_data=f"dl|{key}|audio"),
    )
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data=f"x|{key}"))
    return kb


def card_text(info, note=None):
    text = (
        f"🎬 <b>{esc((info.get('title') or 'Video')[:150])}</b>\n"
        f"👤 {esc(info.get('uploader') or '—')}\n"
        f"⏱ {fmt_duration(info.get('duration'))}\n"
        "━━━━━━━━━━━━━━\n"
    )
    if note:
        text += f"{note}\n\n"
    text += "👇 <b>Choose a format</b>"
    return text


def make_progress_hook(msg):
    last = [0.0]

    def hook(d):
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - last[0] < 3:
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if not total:
            return
        last[0] = now
        pct = min(d.get("downloaded_bytes", 0) / total, 1)
        filled = int(pct * 10)
        bar = "█" * filled + "░" * (10 - filled)
        speed = d.get("speed")
        sp = f" · {speed / 1024 / 1024:.1f} MB/s" if speed else ""
        set_text(msg, f"⬇️ <b>Downloading</b>\n{bar} {pct * 100:.0f}%{sp}")

    return hook


# ───────────────────────── handlers ─────────────────────────
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(
        message,
        "👋 <b>Universal Fast Video Downloader</b>\n"
        "━━━━━━━━━━━━━━\n"
        "Send me a link from:\n\n"
        "▶️ YouTube / Shorts\n"
        "🎵 TikTok\n"
        "📸 Instagram\n"
        "🐦 X (Twitter)\n"
        "📘 Facebook / Reddit\n"
        "…and 1000+ other sites\n\n"
        "⚡ <b>Just paste the link below!</b>",
        parse_mode="HTML",
    )


@bot.message_handler(func=lambda m: m.text and URL_RE.search(m.text))
def handle_link(message):
    url = URL_RE.search(message.text).group(0)
    chat_id = message.chat.id
    status = bot.reply_to(message, "🔎 Fetching video info...")

    try:
        opts = base_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        set_text(status, friendly_error(e))
        return
    except Exception as e:
        set_text(status, f"❌ An error occurred: {esc(str(e)[:100])}")
        return

    prune_pending()
    key = uuid.uuid4().hex[:8]
    with PENDING_LOCK:
        PENDING[key] = {"url": url, "t": time.time(), "busy": False, "info": {
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
        }}

    text = card_text(info)
    kb = quality_keyboard(key)
    thumb = info.get("thumbnail")

    sent = None
    if thumb and str(thumb).startswith("http"):
        try:
            sent = bot.send_photo(
                chat_id, thumb, caption=text, parse_mode="HTML", reply_markup=kb
            )
        except Exception:
            sent = None
    if sent is None:
        sent = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)

    try:
        bot.delete_message(chat_id, status.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("x|"))
def on_cancel(call):
    key = call.data.split("|")[1]
    with PENDING_LOCK:
        PENDING.pop(key, None)
    bot.answer_callback_query(call.id, "Cancelled")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("dl|"))
def on_download(call):
    _, key, q = call.data.split("|")
    msg = call.message
    chat_id = msg.chat.id

    with PENDING_LOCK:
        item = PENDING.get(key)
        if item and item["busy"]:
            bot.answer_callback_query(call.id, "Already working on it ⏳")
            return
        if item:
            item["busy"] = True

    if not item:
        bot.answer_callback_query(call.id, "Expired — please send the link again.", show_alert=True)
        return

    bot.answer_callback_query(call.id, "Starting… ⚡")
    set_text(msg, "⏳ <b>Starting download...</b>")

    workdir = tempfile.mkdtemp(prefix="dl_")
    saved_info = item["info"]
    try:
        opts = base_opts()
        opts.update({
            "format": fmt_selector(q),
            "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "max_filesize": MAX_FILE_SIZE,
            "concurrent_fragment_downloads": 16,
            "http_chunk_size": 10 * 1024 * 1024,
            "progress_hooks": [make_progress_hook(msg)],
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(item["url"], download=True)

        title = (info.get("title") or "Video")[:200]

        files = [
            os.path.join(workdir, f)
            for f in os.listdir(workdir)
            if not f.endswith((".part", ".ytdl", ".temp"))
        ]
        path = max(files, key=os.path.getsize) if files else None

        if not path or os.path.getsize(path) > MAX_FILE_SIZE:
            set_text(
                msg,
                card_text(saved_info, "⚠️ <b>Too big for Telegram (50 MB).</b> Pick a lower quality:"),
                quality_keyboard(key),
            )
            return

        set_text(msg, "🚀 <b>Uploading to Telegram...</b>")

        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            if q == "audio" or ext in (".m4a", ".mp3", ".opus", ".ogg", ".aac"):
                bot.send_audio(
                    chat_id,
                    f,
                    caption=f"🎵 {title}",
                    title=title,
                    performer=info.get("uploader"),
                    duration=int(info["duration"]) if info.get("duration") else None,
                    timeout=300,
                )
            elif ext in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
                bot.send_video(
                    chat_id,
                    f,
                    caption=f"🎬 {title}",
                    supports_streaming=True,
                    width=info.get("width"),
                    height=info.get("height"),
                    duration=int(info["duration"]) if info.get("duration") else None,
                    timeout=300,
                )
            elif ext in (".jpg", ".jpeg", ".png", ".webp"):
                bot.send_photo(chat_id, f, caption=title, timeout=300)
            else:
                bot.send_document(chat_id, f, caption=title, timeout=300)

        with PENDING_LOCK:
            PENDING.pop(key, None)
        try:
            bot.delete_message(chat_id, msg.message_id)
        except Exception:
            pass

    except yt_dlp.utils.DownloadError as e:
        m = str(e).lower()
        if "larger than" in m or "max-filesize" in m:
            set_text(
                msg,
                card_text(saved_info, "⚠️ <b>Too big for Telegram (50 MB).</b> Pick a lower quality:"),
                quality_keyboard(key),
            )
        else:
            set_text(msg, friendly_error(e))
    except Exception as e:
        set_text(msg, f"❌ An error occurred: {esc(str(e)[:100])}")
    finally:
        with PENDING_LOCK:
            if key in PENDING:
                PENDING[key]["busy"] = False
        shutil.rmtree(workdir, ignore_errors=True)


@bot.message_handler(func=lambda m: True)
def handle_other(message):
    bot.reply_to(message, "Please send a valid video URL starting with http:// or https://")


if __name__ == "__main__":
    setup_cookies()
    start_health_server()
    print("🤖 Fast Bot is running...")
    bot.infinity_polling(skip_pending=True)