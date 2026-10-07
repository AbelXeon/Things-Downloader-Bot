import os
import uuid
import telebot
from dotenv import load_dotenv
import yt_dlp
import imageio_ffmpeg

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN. Please set it as an Environment Variable!")

bot = telebot.TeleBot(BOT_TOKEN)

# 50 MB Telegram Bot API limit in bytes
MAX_FILE_SIZE = 50 * 1024 * 1024

# Locate bundled ffmpeg
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(
        message,
        "👋 **Universal Video Downloader**\n\n"
        "Send me a link from:\n"
        "• YouTube / Shorts\n"
        "• TikTok\n"
        "• Instagram\n"
        "• X (Twitter)\n"
        "• Facebook / Reddit\n\n"
        "⚡ *Just paste the link below!*",
        parse_mode="Markdown"
    )


@bot.message_handler(func=lambda msg: msg.text and msg.text.startswith(('http://', 'https://')))
def handle_link(message):
    url = message.text.strip()
    chat_id = message.chat.id

    status_msg = bot.reply_to(message, "⏳ Fetching and downloading video... Please wait.")

    file_id = str(uuid.uuid4())[:8]
    output_template = f"downloads/{file_id}_%(title).50s.%(ext)s"
    os.makedirs("downloads", exist_ok=True)

    downloaded_file = None

    ydl_opts = {
        'ffmpeg_location': FFMPEG_PATH,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'Video')
            downloaded_file = ydl.prepare_filename(info)

            if not os.path.exists(downloaded_file):
                base, _ = os.path.splitext(downloaded_file)
                if os.path.exists(f"{base}.mp4"):
                    downloaded_file = f"{base}.mp4"

        if os.path.exists(downloaded_file):
            file_size = os.path.getsize(downloaded_file)

            if file_size > MAX_FILE_SIZE:
                bot.edit_message_text(
                    "❌ This video is larger than 50 MB. Telegram bots cannot upload files over 50 MB.",
                    chat_id=chat_id,
                    message_id=status_msg.message_id
                )
                return

            bot.edit_message_text(
                "🚀 Uploading video to Telegram...",
                chat_id=chat_id,
                message_id=status_msg.message_id
            )

            with open(downloaded_file, 'rb') as video:
                bot.send_video(
                    chat_id,
                    video=video,
                    caption=f"🎬 **{title}**",
                    parse_mode="Markdown"
                )

            bot.delete_message(chat_id, status_msg.message_id)

    except yt_dlp.utils.DownloadError as e:
        bot.edit_message_text(
            "❌ Failed to download. The link might be private, blocked by the platform, or age-restricted.",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )
    except Exception as e:
        bot.edit_message_text(
            f"❌ An error occurred: {str(e)[:100]}",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )
    finally:
        if downloaded_file and os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
            except Exception:
                pass


@bot.message_handler(func=lambda msg: True)
def handle_other(message):
    bot.reply_to(message, "Please send a valid video URL starting with `http://` or `https://`.", parse_mode="Markdown")


if __name__ == "__main__":
    print("🤖 Bot is running...")
    bot.infinity_polling()